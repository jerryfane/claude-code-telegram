"""Event handlers that bridge the event bus to Claude and Telegram.

AgentHandler: translates events into ClaudeIntegration.run_command() calls.
NotificationHandler: subscribes to AgentResponseEvent and delivers to Telegram.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from ..claude.facade import ClaudeIntegration
from ..scheduler.heartbeat import HeartbeatService
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
        suppress_quiet_heartbeats: bool = True,
    ) -> None:
        self.event_bus = event_bus
        self.claude = claude_integration
        self.default_working_directory = default_working_directory
        self.default_user_id = default_user_id
        self.heartbeat = heartbeat_service
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

        prompt = event.prompt
        if event.skill_name:
            prompt = (
                f"/{event.skill_name}\n\n{prompt}" if prompt else f"/{event.skill_name}"
            )

        working_dir = event.working_directory or self.default_working_directory

        try:
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=self.default_user_id,
            )

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

            # Phase 2: signals found — invoke Claude with focused prompt
            await self.heartbeat.record_signal(result)
            prompt = result.build_prompt()

            logger.info(
                "Heartbeat found signals, invoking Claude",
                signal_count=len(result.signals),
                max_severity=result.max_severity,
            )

            working_dir = event.working_directory or self.default_working_directory
            response = await self.claude.run_command(
                prompt=prompt,
                working_directory=working_dir,
                user_id=self.default_user_id,
            )

            if response.content:
                header = (
                    f"🫀 <b>Heartbeat — {len(result.signals)} signal(s)</b>\n\n"
                )
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
