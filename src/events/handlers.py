"""Event handlers that bridge the event bus to Claude and Telegram.

AgentHandler: translates events into ClaudeIntegration.run_command() calls.
NotificationHandler: subscribes to AgentResponseEvent and delivers to Telegram.
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from ..claude.facade import ClaudeIntegration
from ..scheduler.heartbeat import HeartbeatService
from ..scheduler.memory_sync import MemorySyncService
from ..scheduler.moltbook_notify import MoltbookNotifyService
from ..scheduler.moltbook_stats import MoltbookStatsService
from ..scheduler.reminder import ReminderService
from ..scheduler.x_digest import XDigestService
from ..scheduler.x_mentions import XMentionsService
from ..scheduler.x_stats import XStatsService
from .bus import Event, EventBus
from .types import AgentResponseEvent, ScheduledEvent, WebhookEvent

logger = structlog.get_logger()


class AgentHandler:
    """Translates incoming events into Claude agent executions.

    Webhook and scheduled events are converted into prompts and sent
    to ClaudeIntegration.run_command(). The response is published
    back as an AgentResponseEvent for delivery.

    Heartbeat jobs (skill_name="heartbeat") run cheap Phase 1 checks
    first and only invoke Claude if signals are detected.
    """

    def __init__(
        self,
        event_bus: EventBus,
        claude_integration: ClaudeIntegration,
        default_working_directory: Path,
        default_user_id: int = 0,
        heartbeat_service: Optional[HeartbeatService] = None,
        x_digest_service: Optional[XDigestService] = None,
        moltbook_stats_service: Optional[MoltbookStatsService] = None,
        moltbook_notify_service: Optional[MoltbookNotifyService] = None,
        x_mentions_service: Optional[XMentionsService] = None,
        x_stats_service: Optional[XStatsService] = None,
        reminder_service: Optional[ReminderService] = None,
        memory_sync_service: Optional[MemorySyncService] = None,
        storage: Optional[Any] = None,
        suppress_quiet_heartbeats: bool = True,
    ) -> None:
        self.event_bus = event_bus
        self.claude = claude_integration
        self.default_working_directory = default_working_directory
        self.default_user_id = default_user_id
        self.storage = storage
        self.heartbeat = heartbeat_service
        self.x_digest = x_digest_service
        self.moltbook_stats = moltbook_stats_service
        self.moltbook_notify = moltbook_notify_service
        self.x_mentions = x_mentions_service
        self.x_stats = x_stats_service
        self.reminder = reminder_service
        self.memory_sync = memory_sync_service
        self.suppress_quiet_heartbeats = suppress_quiet_heartbeats

    def register(self) -> None:
        """Subscribe to events that need agent processing."""
        self.event_bus.subscribe(WebhookEvent, self.handle_webhook)
        self.event_bus.subscribe(ScheduledEvent, self.handle_scheduled)

    async def handle_webhook(self, event: Event) -> None:
        """Process a webhook event through Claude."""
        if not isinstance(event, WebhookEvent):
            return

        logger.info(
            "Processing webhook event through agent",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )

        prompt = self._build_webhook_prompt(event)

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=self.default_working_directory,
                user_id=self.default_user_id,
            )
            self._fire_memory_sync()
            await self._log_scheduled_interaction(prompt, response, self.default_user_id)

            if response.content:
                # We don't know which chat to send to from a webhook alone.
                # The notification service needs configured target chats.
                # Publish with chat_id=0 — the NotificationService
                # will broadcast to configured notification_chat_ids.
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=0,
                        text=response.content,
                        originating_event_id=event.id,
                    )
                )
        except Exception:
            logger.exception(
                "Agent execution failed for webhook event",
                provider=event.provider,
                event_id=event.id,
            )

    async def handle_scheduled(self, event: Event) -> None:
        """Process a scheduled event — heartbeat jobs get Phase 1 gating."""
        if not isinstance(event, ScheduledEvent):
            return

        logger.info(
            "Processing scheduled event through agent",
            job_id=event.job_id,
            job_name=event.job_name,
        )

        # Heartbeat jobs: run cheap checks first, only invoke Claude if needed
        if event.skill_name == "heartbeat" and self.heartbeat:
            await self._handle_heartbeat(event)
            return

        # X/Twitter digest: search then summarize with Claude
        if event.skill_name == "x_digest" and self.x_digest:
            await self._handle_x_digest(event)
            return

        # Reminder: one-shot delivery, no Claude needed ($0)
        if event.skill_name == "reminder":
            await self._handle_reminder(event)
            return

        # Moltbook notify: lightweight check, only invoke Claude if unread > 0
        if event.skill_name == "moltbook_notify" and self.moltbook_notify:
            await self._handle_moltbook_notify(event)
            return

        # Moltbook stats: poll API for post performance (no Claude needed)
        if event.skill_name == "moltbook_stats" and self.moltbook_stats:
            await self._handle_moltbook_stats(event)
            return

        # X mentions: check for @mentions (no Claude needed)
        if event.skill_name == "x_mentions" and self.x_mentions:
            await self._handle_x_mentions(event)
            return

        # X stats: daily stats summary (no Claude needed)
        if event.skill_name == "x_stats" and self.x_stats:
            await self._handle_x_stats(event)
            return

        # X lurk: read feed then engage via Claude
        if event.skill_name == "x_lurk":
            await self._handle_x_lurk(event)
            return

        prompt = event.prompt
        if event.skill_name:
            prompt = (
                f"/{event.skill_name}\n\n{prompt}" if prompt else f"/{event.skill_name}"
            )

        working_dir = event.working_directory or self.default_working_directory

        try:
            user_id = event.user_id or self.default_user_id
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=user_id,
            )
            self._fire_memory_sync()
            await self._log_scheduled_interaction(prompt, response, user_id)

            if response.content:
                await self._broadcast_response(
                    event.target_chat_ids, response.content, event.id
                )
        except Exception:
            logger.exception(
                "Agent execution failed for scheduled event",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_heartbeat(self, event: ScheduledEvent) -> None:
        """Two-phase heartbeat: cheap checks first, Claude only if signals found."""
        assert self.heartbeat is not None

        try:
            result = await self.heartbeat.run_checks()

            if not result.has_signals:
                # Phase 1 all-clear: write timestamp, skip Claude, $0
                await self.heartbeat.record_quiet()
                if not self.suppress_quiet_heartbeats:
                    await self._broadcast_response(
                        event.target_chat_ids,
                        "🫀 Heartbeat: all quiet. No action needed.",
                        event.id,
                    )
                return

            # Dedup: suppress Claude if signals haven't changed since last check
            if self.heartbeat.is_duplicate_signal(result):
                await self.heartbeat.record_signal(result)
                logger.info(
                    "Heartbeat signals unchanged, skipping Claude",
                    signal_count=len(result.signals),
                )
                return

            # Phase 2: signals found — invoke Claude with focused prompt
            await self.heartbeat.record_signal(result)
            prompt = result.build_prompt()

            logger.info(
                "Heartbeat found signals, invoking Claude",
                signal_count=len(result.signals),
                max_severity=result.max_severity,
            )

            working_dir = event.working_directory or self.default_working_directory
            user_id = event.user_id or self.default_user_id
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=user_id,
            )
            self._fire_memory_sync()
            await self._log_scheduled_interaction(prompt, response, user_id)

            if response.content:
                header = f"🫀 <b>Heartbeat — {len(result.signals)} signal(s)</b>\n\n"
                await self._broadcast_response(
                    event.target_chat_ids,
                    header + response.content,
                    event.id,
                )

        except Exception:
            logger.exception(
                "Heartbeat check failed",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_x_digest(self, event: ScheduledEvent) -> None:
        """Run X/Twitter digest: search tweets, then summarize with Claude."""
        assert self.x_digest is not None

        try:
            result = await self.x_digest.run()

            if result.error and not result.has_results:
                logger.error("X digest failed", error=result.error)
                await self._broadcast_response(
                    event.target_chat_ids,
                    f"X digest failed: {result.error}",
                    event.id,
                )
                return

            if not result.has_results:
                logger.info("X digest returned no tweets")
                await self._broadcast_response(
                    event.target_chat_ids,
                    "X digest: no tweets found for any topic.",
                    event.id,
                )
                return

            # Summarize with Claude
            prompt = result.build_prompt()
            logger.info(
                "X digest found tweets, invoking Claude",
                total_tweets=result.total_tweets,
                topic_count=len(result.topics),
            )

            working_dir = event.working_directory or self.default_working_directory
            user_id = event.user_id or self.default_user_id
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=user_id,
            )
            self._fire_memory_sync()
            await self._log_scheduled_interaction(prompt, response, user_id)

            if response.content:
                header = (
                    f"📰 <b>X/Twitter Daily Digest — "
                    f"{result.total_tweets} tweets across "
                    f"{len(result.topics)} topic(s)</b>\n\n"
                )
                await self._broadcast_response(
                    event.target_chat_ids,
                    header + response.content,
                    event.id,
                )

        except Exception:
            logger.exception(
                "X digest handler failed",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_moltbook_notify(self, event: ScheduledEvent) -> None:
        """Two-phase Moltbook notification check: cheap API call first, Claude only if unread."""
        assert self.moltbook_notify is not None

        try:
            result = await self.moltbook_notify.check()

            if result.error:
                logger.error("Moltbook notify check failed", error=result.error)
                return

            if not result.has_notifications:
                # All quiet — no Claude invocation, $0
                logger.info(
                    "Moltbook notify: no unread notifications",
                    karma=result.karma,
                )
                return

            # Unread notifications — invoke Claude to respond
            prompt = result.build_prompt()
            logger.info(
                "Moltbook notify: unread notifications, invoking Claude",
                unread_count=result.unread_count,
            )

            working_dir = event.working_directory or self.default_working_directory
            user_id = event.user_id or self.default_user_id
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=user_id,
            )
            self._fire_memory_sync()
            await self._log_scheduled_interaction(prompt, response, user_id)

            if response.content:
                header = (
                    f"🔔 <b>Moltbook — {result.unread_count} notification(s)</b>\n\n"
                )
                await self._broadcast_response(
                    event.target_chat_ids,
                    header + response.content,
                    event.id,
                )

        except Exception:
            logger.exception(
                "Moltbook notify handler failed",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_moltbook_stats(self, event: ScheduledEvent) -> None:
        """Poll Moltbook API for post stats — no Claude needed, pure data."""
        assert self.moltbook_stats is not None

        try:
            result = await self.moltbook_stats.run()

            if result.error and not result.has_results:
                logger.error("Moltbook stats failed", error=result.error)
                await self._broadcast_response(
                    event.target_chat_ids,
                    f"Moltbook stats failed: {result.error}",
                    event.id,
                )
                return

            if not result.has_results:
                logger.info("Moltbook stats: no posts tracked")
                await self._broadcast_response(
                    event.target_chat_ids,
                    "Moltbook stats: no posts tracked yet.",
                    event.id,
                )
                return

            summary = result.build_summary()
            header = f"📊 <b>Moltbook Performance</b>\n\n"
            await self._broadcast_response(
                event.target_chat_ids,
                header + summary,
                event.id,
            )

        except Exception:
            logger.exception(
                "Moltbook stats handler failed",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_x_mentions(self, event: ScheduledEvent) -> None:
        """Check X mentions — no Claude, pure script ($0)."""
        assert self.x_mentions is not None

        try:
            result = await self.x_mentions.run()

            if result.error:
                logger.error("X mentions check failed", error=result.error)
                return

            if not result.has_mentions:
                logger.info("X mentions: no new mentions")
                return  # Silent — don't spam if nothing new

            await self._broadcast_response(
                event.target_chat_ids,
                result.summary,
                event.id,
            )

        except Exception:
            logger.exception(
                "X mentions handler failed",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_x_stats(self, event: ScheduledEvent) -> None:
        """Daily X stats summary — no Claude, pure script ($0)."""
        assert self.x_stats is not None

        try:
            result = await self.x_stats.run()

            if result.error:
                logger.error("X stats failed", error=result.error)
                await self._broadcast_response(
                    event.target_chat_ids,
                    f"X stats failed: {result.error}",
                    event.id,
                )
                return

            if not result.has_results:
                logger.info("X stats: no results")
                return

            await self._broadcast_response(
                event.target_chat_ids,
                result.summary,
                event.id,
            )

        except Exception:
            logger.exception(
                "X stats handler failed",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_x_lurk(self, event: ScheduledEvent) -> None:
        """Read X feed then engage via Claude — two-phase like x_digest."""
        import asyncio as _asyncio
        import json
        import sys as _sys

        try:
            # Phase 1: Get feed data via x_post.py
            script_path = self.default_working_directory / "scripts" / "x_post.py"
            proc = await _asyncio.create_subprocess_exec(
                _sys.executable,
                str(script_path),
                "feed",
                "--limit", "20",
                cwd=str(self.default_working_directory),
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            stdout, stderr = await _asyncio.wait_for(
                proc.communicate(), timeout=60
            )
            feed_text = stdout.decode().strip()

            if proc.returncode != 0 or not feed_text:
                logger.error("X lurk feed fetch failed", stderr=stderr.decode()[:200])
                return

            feed_data = json.loads(feed_text)
            tweets = feed_data.get("tweets", [])
            if not tweets:
                logger.info("X lurk: empty feed")
                return

            # Phase 2: Ask Claude to analyze and engage
            feed_summary = "\n".join(
                f"- @{t['author']}: \"{t['text'][:200]}\" "
                f"({t['favorite_count']}♥ {t['retweet_count']}🔁) "
                f"→ {t['url']}"
                for t in tweets[:20]
            )

            prompt = (
                f"Here's the current X/Twitter feed ({len(tweets)} tweets):\n\n"
                f"{feed_summary}\n\n"
                f"Review the feed. Find 1-2 interesting threads to engage with — "
                f"prioritize AI agents, builders, technical content, or anything "
                f"related to our work (Claude on Raspberry Pi, Moltbook, autonomous agents). "
                f"If you find something worth engaging, use scripts/x_post.py to reply or quote. "
                f"Keep replies concise, genuine, and technical. If nothing is worth engaging "
                f"with, just say so — don't force it."
            )

            working_dir = event.working_directory or self.default_working_directory
            user_id = event.user_id or self.default_user_id
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=user_id,
            )
            self._fire_memory_sync()
            await self._log_scheduled_interaction(prompt, response, user_id)

            if response.content:
                header = f"👀 <b>X Lurk — {len(tweets)} tweets scanned</b>\n\n"
                await self._broadcast_response(
                    event.target_chat_ids,
                    header + response.content,
                    event.id,
                )

        except Exception:
            logger.exception(
                "X lurk handler failed",
                job_id=event.job_id,
                event_id=event.id,
            )

    async def _handle_reminder(self, event: ScheduledEvent) -> None:
        """Deliver a one-shot reminder — no Claude, just send the message ($0)."""
        message = event.prompt
        if not message:
            return

        # Check for Relay delivery prefix
        use_relay = message.startswith("[RELAY]")
        if use_relay:
            message = message[7:]  # Strip prefix

        if use_relay:
            success = await self._send_relay_alert(message)
            if success:
                logger.info("Reminder delivered via Relay", job_id=event.job_id)
                return
            logger.warning("Relay delivery failed, falling back to Telegram")

        # Telegram delivery (default or fallback)
        header = "🔔 <b>Reminder</b>\n\n"
        await self._broadcast_response(
            event.target_chat_ids,
            header + message,
            event.id,
        )
        logger.info("Reminder delivered via Telegram", job_id=event.job_id)
        # Auto-deactivation happens in scheduler._fire_reminder()

    async def _send_relay_alert(self, message: str) -> bool:
        """POST to Relay API to send a phone alert. Returns True on success."""
        import json as _json
        from pathlib import Path as _Path

        import httpx

        creds_path = _Path(__file__).resolve().parent.parent.parent / "data" / "relay_credentials.json"
        if not creds_path.exists():
            logger.warning("Relay credentials not found", path=str(creds_path))
            return False
        try:
            creds = _json.loads(creds_path.read_text())
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{creds['base_url']}/execute",
                    headers={"Authorization": f"Bearer {creds['api_key']}"},
                    json={
                        "service": "alerts",
                        "action": "send",
                        "params": {
                            "message": f"🔔 Reminder: {message}",
                            "title": "Phobos Reminder",
                            "priority": "normal",
                        },
                    },
                    timeout=10,
                )
                return r.status_code == 200
        except Exception as e:
            logger.warning("Relay alert failed", error=str(e))
            return False

    async def _log_scheduled_interaction(
        self, prompt: str, response: Any, user_id: int
    ) -> None:
        """Log a scheduled job's Claude interaction to the messages table."""
        if not self.storage:
            return
        try:
            await self.storage.save_claude_interaction(
                user_id=user_id,
                session_id=getattr(response, "session_id", "scheduled"),
                prompt=prompt,
                response=response,
                ip_address=None,
            )
        except Exception:
            logger.debug("Failed to log scheduled interaction", exc_info=True)

    def _fire_memory_sync(self) -> None:
        """Fire-and-forget memory sync and reminder processing after Claude response."""
        if self.memory_sync:
            asyncio.create_task(self.memory_sync.sync_if_needed())
        if self.reminder:
            asyncio.create_task(self.reminder.process_pending())

    async def _broadcast_response(
        self,
        target_chat_ids: List[int],
        text: str,
        originating_event_id: Optional[str],
    ) -> None:
        """Publish response to target chats or broadcast to defaults."""
        if target_chat_ids:
            for chat_id in target_chat_ids:
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=chat_id,
                        text=text,
                        originating_event_id=originating_event_id,
                    )
                )
        else:
            await self.event_bus.publish(
                AgentResponseEvent(
                    chat_id=0,
                    text=text,
                    originating_event_id=originating_event_id,
                )
            )

    def _build_webhook_prompt(self, event: WebhookEvent) -> str:
        """Build a Claude prompt from a webhook event."""
        payload_summary = self._summarize_payload(event.payload)

        return (
            f"A {event.provider} webhook event occurred.\n"
            f"Event type: {event.event_type_name}\n"
            f"Payload summary:\n{payload_summary}\n\n"
            f"Analyze this event and provide a concise summary. "
            f"Highlight anything that needs my attention."
        )

    def _summarize_payload(self, payload: Dict[str, Any], max_depth: int = 2) -> str:
        """Create a readable summary of a webhook payload."""
        lines: List[str] = []
        self._flatten_dict(payload, lines, max_depth=max_depth)
        # Cap at 2000 chars to keep prompt reasonable
        summary = "\n".join(lines)
        if len(summary) > 2000:
            summary = summary[:2000] + "\n... (truncated)"
        return summary

    def _flatten_dict(
        self,
        data: Any,
        lines: list,
        prefix: str = "",
        depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        """Flatten a nested dict into key: value lines."""
        if depth >= max_depth:
            lines.append(f"{prefix}: ...")
            return

        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    self._flatten_dict(value, lines, full_key, depth + 1, max_depth)
                else:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    lines.append(f"{full_key}: {val_str}")
        elif isinstance(data, list):
            lines.append(f"{prefix}: [{len(data)} items]")
            for i, item in enumerate(data[:3]):  # Show first 3 items
                self._flatten_dict(item, lines, f"{prefix}[{i}]", depth + 1, max_depth)
        else:
            lines.append(f"{prefix}: {data}")
