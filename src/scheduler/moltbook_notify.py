"""Lightweight Moltbook notification checker — two-phase like heartbeat.

Phase 1 (Python, $0): Hit /home, check unread_notification_count.
Phase 2 (Claude, only if needed): Process notifications and reply.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

_DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "moltbook_api.py"
_DEFAULT_CREDENTIALS = Path(__file__).resolve().parent.parent.parent / "data" / "moltbook_credentials.json"


@dataclass
class MoltbookNotifyResult:
    """Result of the lightweight notification check."""

    has_notifications: bool = False
    unread_count: int = 0
    notifications: List[Dict[str, Any]] = field(default_factory=list)
    activity_on_posts: List[Dict[str, Any]] = field(default_factory=list)
    karma: int = 0
    error: Optional[str] = None

    def build_prompt(self) -> str:
        """Build a Claude prompt from the notification data."""
        lines = [
            f"You are Phobos (phobosintern on Moltbook). "
            f"You have {self.unread_count} unread notification(s). Reply to each one.\n",
            "TOOLS:",
            '  poetry run python scripts/moltbook_api.py comment <post_id> "your reply"',
            "  poetry run python scripts/moltbook_api.py comments <post_id> --sort new",
            "  poetry run python scripts/moltbook_api.py mark-read",
            "",
            "NOTIFICATIONS:",
        ]

        for n in self.notifications[:20]:  # Cap at 20
            ntype = n.get("type", "?")
            # Handle both API formats
            post_info = n.get("post", {}) if isinstance(n.get("post"), dict) else {}
            post_title = post_info.get("title", "?")
            post_id = post_info.get("id", n.get("relatedPostId", "?"))
            comment_id = n.get("relatedCommentId", "")
            content = n.get("content", n.get("preview", ""))[:200]
            is_read = n.get("isRead", False)
            read_marker = "" if not is_read else " [read]"
            lines.append(
                f"- [{ntype}]{read_marker} on \"{post_title}\" (post_id: {post_id})"
            )
            if comment_id:
                lines.append(f"  comment_id: {comment_id}")
            if content:
                lines.append(f"  > {content}")

        if self.activity_on_posts:
            lines.append("\nACTIVITY ON YOUR POSTS:")
            for a in self.activity_on_posts:
                title = a.get("post_title", "?")
                count = a.get("new_notification_count", 0)
                commenters = a.get("latest_commenters", [])
                post_id = a.get("post_id", "?")
                lines.append(
                    f"- \"{title}\" (post_id: {post_id}): "
                    f"{count} new notification(s) from {', '.join(commenters[:3])}"
                )

        lines.extend([
            "",
            "INSTRUCTIONS:",
            "1. For each notification, read context if needed (use `comments` command)",
            "2. Reply with 2-4 sentences — dry wit, concise, builder perspective",
            "3. Mark all read when done: run `mark-read`",
        ])

        return "\n".join(lines)


class MoltbookNotifyService:
    """Two-phase notification checker for Moltbook.

    Phase 1: Hit /home endpoint, check unread count (Python, no LLM).
    Phase 2: Only invoke Claude if notifications exist.
    """

    def __init__(
        self,
        working_directory: Path,
        script_path: Optional[Path] = None,
        credentials_path: Optional[Path] = None,
    ) -> None:
        self.working_directory = working_directory
        self.script_path = script_path or _DEFAULT_SCRIPT
        self.credentials_path = credentials_path or _DEFAULT_CREDENTIALS

    async def check(self) -> MoltbookNotifyResult:
        """Phase 1: Check for unread notifications without invoking Claude."""
        if not self.script_path.exists():
            return MoltbookNotifyResult(error=f"Script not found: {self.script_path}")

        # Fetch /home
        home_data = await self._run_command("home")
        if home_data is None:
            return MoltbookNotifyResult(error="Failed to fetch /home")

        account = home_data.get("your_account", {})
        unread = account.get("unread_notification_count", 0)
        karma = account.get("karma", 0)
        activity = home_data.get("activity_on_your_posts", [])

        if unread == 0:
            logger.info("moltbook_notify: no unread notifications", karma=karma)
            return MoltbookNotifyResult(karma=karma)

        # Unread > 0: fetch full notifications
        logger.info(
            "moltbook_notify: unread notifications found",
            unread_count=unread,
            karma=karma,
        )
        notif_data = await self._run_command("notifications")
        notifications = []
        if notif_data:
            notifications = notif_data.get("notifications", [])

        return MoltbookNotifyResult(
            has_notifications=True,
            unread_count=unread,
            notifications=notifications,
            activity_on_posts=activity,
            karma=karma,
        )

    async def _run_command(self, command: str, *args: str) -> Optional[Dict[str, Any]]:
        """Run a moltbook_api.py CLI command and parse JSON output."""
        cmd = [
            sys.executable,
            str(self.script_path),
            "--credentials",
            str(self.credentials_path),
            command,
            *args,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.working_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            logger.error("moltbook_notify: command timed out", command=command)
            return None
        except Exception as e:
            logger.error("moltbook_notify: command failed", command=command, error=str(e))
            return None

        if proc.returncode != 0:
            logger.warning(
                "moltbook_notify: command failed",
                command=command,
                returncode=proc.returncode,
                stderr=stderr.decode().strip()[:200],
            )

        stdout_text = stdout.decode().strip()
        if not stdout_text:
            return None

        try:
            return json.loads(stdout_text)
        except json.JSONDecodeError as e:
            logger.error("moltbook_notify: invalid JSON", command=command, error=str(e))
            return None
