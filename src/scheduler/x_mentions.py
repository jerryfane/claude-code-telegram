"""X/Twitter mention checker — shells out to scripts/x_mentions.py.

Runs the standalone mention checker in --dry-run mode, captures output,
and provides formatted summaries for Telegram delivery via the event bus.
Zero Claude tokens.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "x_mentions.py"


@dataclass
class XMentionsResult:
    """Parsed results from the mention checker script."""

    summary: str = ""
    mention_count: int = 0
    error: Optional[str] = None

    @property
    def has_mentions(self) -> bool:
        return self.mention_count > 0


class XMentionsService:
    """Runs the X mention checker script and captures output."""

    def __init__(
        self,
        working_directory: Path,
        script_path: Optional[Path] = None,
    ) -> None:
        self.working_directory = working_directory
        self.script_path = script_path or _DEFAULT_SCRIPT

    async def run(self) -> XMentionsResult:
        """Execute the mention checker in dry-run mode."""
        if not self.script_path.exists():
            return XMentionsResult(error=f"Script not found: {self.script_path}")

        logger.info("Running X mention check", script=str(self.script_path))

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self.script_path),
                "--dry-run",
                cwd=str(self.working_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=60
            )
        except asyncio.TimeoutError:
            logger.error("X mentions script timed out")
            return XMentionsResult(error="Script timed out after 60s")
        except Exception as e:
            logger.error("X mentions script failed", error=str(e))
            return XMentionsResult(error=f"Script execution failed: {e}")

        stdout_text = stdout.decode().strip()
        stderr_text = stderr.decode().strip()

        if proc.returncode != 0:
            logger.error(
                "X mentions script failed",
                returncode=proc.returncode,
                stderr=stderr_text[:500],
            )
            if not stdout_text:
                return XMentionsResult(
                    error=f"Script exited {proc.returncode}: {stderr_text[:200]}"
                )

        # --dry-run prints the formatted message to stdout
        # If "No new mentions" was logged to stderr, stdout will be empty
        if not stdout_text:
            return XMentionsResult(summary="", mention_count=0)

        # Count mentions from the output (header line has count)
        count = 0
        for line in stdout_text.splitlines():
            if "new)" in line:
                try:
                    count = int(line.split("(")[1].split()[0])
                except (IndexError, ValueError):
                    pass

        return XMentionsResult(
            summary=stdout_text,
            mention_count=count,
        )
