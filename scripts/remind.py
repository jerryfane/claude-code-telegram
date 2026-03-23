#!/usr/bin/env python3
"""Reminder CLI — creates, lists, and removes one-shot scheduled reminders.

Claude calls this script via bash to schedule reminders for the user.
Reminders are written to data/pending_reminders.json and picked up by
the bot's ReminderService, which registers them with APScheduler.

Usage:
    python scripts/remind.py add --at "2026-03-24T15:00:00" --message "check deploy" --chat 13218410
    python scripts/remind.py list
    python scripts/remind.py remove <reminder_id>
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PENDING_FILE = DATA_DIR / "pending_reminders.json"
DB_PATH = DATA_DIR / "bot.db"


def _log(msg: str) -> None:
    print(f"[remind] {msg}", file=sys.stderr, flush=True)


def _load_pending() -> list:
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return []


def _save_pending(reminders: list) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(reminders, indent=2) + "\n")


def cmd_add(args: argparse.Namespace) -> None:
    """Add a new reminder."""
    # Parse the target time
    try:
        fire_at = datetime.fromisoformat(args.at)
        # Ensure timezone-aware (assume UTC if naive)
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=UTC)
    except ValueError:
        print(json.dumps({"error": f"Invalid datetime: {args.at}"}))
        sys.exit(1)

    now = datetime.now(UTC)
    if fire_at <= now:
        print(json.dumps({"error": f"Reminder time is in the past: {fire_at.isoformat()}"}))
        sys.exit(1)

    reminder = {
        "id": str(uuid.uuid4()),
        "fire_at": fire_at.isoformat(),
        "message": args.message,
        "chat_id": args.chat,
        "created_at": now.isoformat(),
    }

    pending = _load_pending()
    pending.append(reminder)
    _save_pending(pending)

    # Human-friendly delta
    delta = fire_at - now
    hours = delta.total_seconds() / 3600
    if hours < 1:
        time_str = f"{int(delta.total_seconds() / 60)}m"
    elif hours < 24:
        time_str = f"{hours:.1f}h"
    else:
        time_str = f"{hours / 24:.1f}d"

    result = {
        "success": True,
        "id": reminder["id"],
        "fire_at": reminder["fire_at"],
        "message": reminder["message"],
        "chat_id": reminder["chat_id"],
        "fires_in": time_str,
    }
    print(json.dumps(result))
    _log(f"Reminder created: \"{args.message}\" fires in {time_str} ({fire_at.isoformat()})")


def cmd_list(args: argparse.Namespace) -> None:
    """List pending and active reminders."""
    reminders = []

    # Pending (not yet registered with APScheduler)
    pending = _load_pending()
    for r in pending:
        reminders.append({**r, "status": "pending"})

    # Active in DB (registered with APScheduler)
    try:
        import sqlite3
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT job_id, job_name, cron_expression, prompt, target_chat_ids "
            "FROM scheduled_jobs WHERE skill_name = 'reminder' AND is_active = 1"
        ).fetchall()
        for row in rows:
            chat_ids_str = row["target_chat_ids"] or ""
            chat_ids = [int(x) for x in chat_ids_str.split(",") if x.strip()]
            reminders.append({
                "id": row["job_id"],
                "fire_at": row["cron_expression"],  # ISO datetime stored here
                "message": row["prompt"],
                "chat_id": chat_ids[0] if chat_ids else 0,
                "status": "scheduled",
            })
        db.close()
    except Exception as e:
        _log(f"Could not read DB: {e}")

    # Filter by chat if specified
    if args.chat:
        reminders = [r for r in reminders if r.get("chat_id") == args.chat]

    print(json.dumps({"reminders": reminders, "count": len(reminders)}))


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove a reminder by ID."""
    rid = args.reminder_id
    removed = False

    # Try pending file first
    pending = _load_pending()
    new_pending = [r for r in pending if r.get("id") != rid]
    if len(new_pending) < len(pending):
        _save_pending(new_pending)
        removed = True
        _log(f"Removed from pending: {rid}")

    # Try DB
    if not removed:
        try:
            import sqlite3
            db = sqlite3.connect(str(DB_PATH))
            cursor = db.execute(
                "UPDATE scheduled_jobs SET is_active = 0 WHERE job_id = ? AND skill_name = 'reminder'",
                (rid,),
            )
            if cursor.rowcount > 0:
                removed = True
                _log(f"Deactivated in DB: {rid}")
            db.commit()
            db.close()
        except Exception as e:
            _log(f"DB error: {e}")

    if removed:
        print(json.dumps({"success": True, "id": rid}))
    else:
        print(json.dumps({"error": f"Reminder not found: {rid}"}))
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reminder CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="Create a reminder")
    p_add.add_argument("--at", required=True, help="ISO 8601 datetime (UTC)")
    p_add.add_argument("--message", required=True, help="Reminder message")
    p_add.add_argument("--chat", type=int, required=True, help="Telegram chat ID")

    # list
    p_list = sub.add_parser("list", help="List reminders")
    p_list.add_argument("--chat", type=int, help="Filter by chat ID")

    # remove
    p_remove = sub.add_parser("remove", help="Remove a reminder")
    p_remove.add_argument("reminder_id", help="Reminder ID to remove")

    args = parser.parse_args()
    cmd_map = {
        "add": cmd_add,
        "list": cmd_list,
        "remove": cmd_remove,
    }

    try:
        cmd_map[args.command](args)
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
