"""Reminder service — picks up pending reminders and registers with APScheduler.

Reminders are created by Claude via scripts/remind.py (writes to pending JSON).
This service processes that file after each Claude response and registers
one-shot DateTrigger jobs with the scheduler. When they fire, the handler
delivers the message and deactivates the job.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List

import structlog
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]

logger = structlog.get_logger()

_DEFAULT_PENDING = Path(__file__).resolve().parent.parent.parent / "data" / "pending_reminders.json"


class ReminderService:
    """Processes pending reminders and registers them with the scheduler."""

    def __init__(
        self,
        scheduler: Any,  # JobScheduler — late import to avoid circular
        pending_file: Path = _DEFAULT_PENDING,
    ) -> None:
        self.scheduler = scheduler
        self.pending_file = pending_file

    async def process_pending(self) -> int:
        """Check for pending reminders and register them with APScheduler.

        Called as fire-and-forget after each Claude response.
        Returns count of reminders registered.
        """
        if not self.pending_file.exists():
            return 0

        try:
            pending = json.loads(self.pending_file.read_text())
        except Exception:
            return 0

        if not pending:
            return 0

        registered = 0
        remaining: List[Dict[str, Any]] = []

        for reminder in pending:
            try:
                fire_at = datetime.fromisoformat(reminder["fire_at"])
                if fire_at.tzinfo is None:
                    fire_at = fire_at.replace(tzinfo=UTC)

                # Skip if already past
                if fire_at <= datetime.now(UTC):
                    logger.warning(
                        "Skipping expired reminder",
                        id=reminder.get("id"),
                        fire_at=reminder["fire_at"],
                    )
                    continue

                message = reminder.get("message", "")
                chat_id = reminder.get("chat_id", 0)
                reminder_id = reminder.get("id", "")

                # Register with APScheduler via DateTrigger
                await self.scheduler.add_reminder(
                    reminder_id=reminder_id,
                    reminder_name=f"Reminder: {message[:50]}",
                    fire_at=fire_at,
                    message=message,
                    target_chat_ids=[chat_id] if chat_id else [],
                )
                registered += 1
                logger.info(
                    "Registered reminder with scheduler",
                    id=reminder_id,
                    fire_at=fire_at.isoformat(),
                    message=message[:50],
                )

            except Exception:
                logger.exception(
                    "Failed to register reminder",
                    id=reminder.get("id"),
                )
                remaining.append(reminder)

        # Clear processed reminders from pending file
        try:
            if remaining:
                self.pending_file.write_text(json.dumps(remaining, indent=2) + "\n")
            else:
                self.pending_file.write_text("[]\n")
        except Exception:
            logger.exception("Failed to update pending reminders file")

        return registered
