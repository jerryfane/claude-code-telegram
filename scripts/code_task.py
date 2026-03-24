#!/usr/bin/env python3
"""Code task CLI — queues code agent tasks for the bot to pick up.

Phobos calls this during conversation to spawn a code agent asynchronously.
Tasks are written to data/pending_code_tasks.json and picked up by
CodeAgentService after the current Claude response completes.

Usage:
    python scripts/code_task.py add --task "fix the solver" --chat 13218410 --mode build
    python scripts/code_task.py add --task "explore codebase" --chat 13218410 --mode plan
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PENDING_FILE = DATA_DIR / "pending_code_tasks.json"


def _load_pending() -> list:
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return []


def _save_pending(tasks: list) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(tasks, indent=2) + "\n")


def cmd_add(args: argparse.Namespace) -> None:
    task = {
        "id": str(uuid.uuid4()),
        "task": args.task,
        "chat_id": args.chat,
        "mode": args.mode,
        "created_at": datetime.now(UTC).isoformat(),
    }

    pending = _load_pending()
    pending.append(task)
    _save_pending(pending)

    print(json.dumps({
        "success": True,
        "id": task["id"],
        "task": task["task"][:80],
        "chat_id": task["chat_id"],
        "mode": task["mode"],
    }))


def main() -> None:
    parser = argparse.ArgumentParser(description="Code task CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Queue a code agent task")
    p_add.add_argument("--task", required=True, help="Task description")
    p_add.add_argument("--chat", type=int, required=True, help="Telegram chat ID")
    p_add.add_argument("--mode", default="build", choices=["build", "plan"],
                       help="Permission mode (default: build)")

    args = parser.parse_args()
    try:
        {"add": cmd_add}[args.command](args)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
