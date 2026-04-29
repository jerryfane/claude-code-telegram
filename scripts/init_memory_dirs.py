#!/usr/bin/env python3
"""Bootstrap per-user memory directories for the in-process MCP memory tool.

For each ``.memory/users/{telegram_id}/`` directory, ensures a ``memory/``
subdirectory exists with an empty ``MEMORY.md`` index inside. Idempotent —
existing files are never overwritten.

Run after upgrading to claude-agent-sdk 0.1.71+:

    poetry run python scripts/init_memory_dirs.py
    poetry run python scripts/init_memory_dirs.py /custom/.memory  # override

If MEMORY_DIR env var is unset and no argument is passed, defaults to
``./.memory`` relative to the current working directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

INDEX_TEMPLATE = """# Memory Index

This file is the index of long-term facts the assistant has learned.
Subdirectories under `/memories/` hold deeper records:

- `/memories/people/` — individual contacts and what we know about them
- `/memories/projects/` — ongoing or past projects
- `/memories/topics/` — recurring topics of conversation
- `/memories/decisions/` — decisions made, with date and reasoning

Add entries as `- short fact (link to file if expanded)`.
"""


def init_user_memory_dir(user_root: Path) -> tuple[bool, bool]:
    """Bootstrap a single user's memory/ subdir.

    Returns (created_dir, created_index). Existing files are left alone.
    """
    memory_subdir = user_root / "memory"
    created_dir = not memory_subdir.exists()
    memory_subdir.mkdir(parents=True, exist_ok=True)

    index_path = memory_subdir / "MEMORY.md"
    created_index = not index_path.exists()
    if created_index:
        index_path.write_text(INDEX_TEMPLATE, encoding="utf-8")

    return created_dir, created_index


def main(memory_dir: Path) -> int:
    if not memory_dir.is_dir():
        print(f"Memory directory not found: {memory_dir}", file=sys.stderr)
        print(
            "Set MEMORY_DIR or pass a path explicitly. "
            "If this is a fresh install, create the directory first.",
            file=sys.stderr,
        )
        return 1

    users_root = memory_dir / "users"
    if not users_root.is_dir():
        print(f"No users directory at {users_root} — nothing to do.")
        return 0

    user_dirs = [p for p in sorted(users_root.iterdir()) if p.is_dir()]
    if not user_dirs:
        print(f"No user subdirs in {users_root} — nothing to do.")
        return 0

    print(f"Bootstrapping memory/ subdirs under {users_root}\n")
    for user_dir in user_dirs:
        created_dir, created_index = init_user_memory_dir(user_dir)
        flag_d = "+" if created_dir else " "
        flag_i = "+" if created_index else " "
        print(f"  [{flag_d}dir {flag_i}index]  {user_dir.name}")
    print("\nDone. (+ = created, blank = already present)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1]).expanduser().resolve()
    elif env := os.environ.get("MEMORY_DIR"):
        target = Path(env).expanduser().resolve()
    else:
        target = Path.cwd() / ".memory"

    sys.exit(main(target))
