"""Event-driven memory sync — pushes .memory/ changes to GitHub immediately.

Complements the 6-hour cron safety net with fire-and-forget sync after
every Claude invocation, so memory writes survive unexpected reboots.
"""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Minimum seconds between syncs to prevent git thrashing
DEBOUNCE_SECONDS = 60


class MemorySyncService:
    """Lightweight git sync for the .memory/ repo.

    Called as a fire-and-forget background task after Claude responses.
    All errors are swallowed — the 6h cron catches anything that slips.
    """

    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir = memory_dir
        self._last_sync: float = 0
        self._syncing = False  # Prevent concurrent syncs

    async def sync_if_needed(self) -> bool:
        """Check for dirty .memory/ files and push if found.

        Returns True if a sync was performed, False otherwise.
        Never raises — all errors are logged and swallowed.
        """
        # Debounce: skip if synced recently
        now = time.time()
        if now - self._last_sync < DEBOUNCE_SECONDS:
            return False

        # Prevent concurrent syncs
        if self._syncing:
            return False
        self._syncing = True

        try:
            return await self._do_sync()
        except Exception:
            logger.exception("memory_sync: unexpected error")
            return False
        finally:
            self._syncing = False

    async def _do_sync(self) -> bool:
        """Internal sync implementation."""
        # 1. Check for uncommitted changes
        dirty = await self._git("status", "--porcelain")
        if dirty is None or not dirty.strip():
            return False

        logger.info("memory_sync: changes detected, syncing")

        # 2. Stage all changes
        if await self._git("add", "-A") is None:
            return False

        # 3. Commit
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = await self._git(
            "commit", "-m", f"chore(memory-sync): auto-sync {ts}"
        )
        if result is None:
            return False

        # 4. Push
        branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        if branch is None:
            branch = "main"
        branch = branch.strip()

        result = await self._git("push", "origin", branch)
        if result is None:
            logger.warning("memory_sync: push failed, 6h cron will retry")
            return False

        self._last_sync = time.time()
        logger.info("memory_sync: pushed to origin/%s", branch)
        return True

    async def _git(self, *args: str) -> str | None:
        """Run a git command in the memory directory.

        Returns stdout on success, None on failure.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=str(self.memory_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=15
            )
            if proc.returncode != 0:
                logger.warning(
                    "memory_sync: git %s failed (rc=%d): %s",
                    args[0], proc.returncode,
                    stderr.decode().strip()[:200],
                )
                return None
            return stdout.decode()
        except asyncio.TimeoutError:
            logger.warning("memory_sync: git %s timed out", args[0])
            return None
        except Exception:
            logger.exception("memory_sync: git %s error", args[0])
            return None
