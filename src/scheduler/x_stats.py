"""X/Twitter daily stats — shells out to scripts/x_stats.py.

Runs the standalone stats script in --dry-run mode, captures output,
and provides formatted summaries for Telegram delivery via the event bus.
Zero Claude tokens.
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "x_stats.py"


@dataclass
class XStatsResult:
    """Parsed results from the stats script."""

    summary: str = ""
    error: Optional[str] = None

    @property
    def has_results(self) -> bool:
        return len(self.summary) > 0


class XStatsService:
    """Runs the X stats script and captures output."""

    def __init__(
        self,
        working_directory: Path,
        script_path: Optional[Path] = None,
    ) -> None:
        self.working_directory = working_directory
        self.script_path = script_path or _DEFAULT_SCRIPT

    async def run(self) -> XStatsResult:
        """Execute the stats script in dry-run mode."""
        if not self.script_path.exists():
            return XStatsResult(error=f"Script not found: {self.script_path}")

        logger.info("Running X stats", script=str(self.script_path))

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
            logger.error("X stats script timed out")
            return XStatsResult(error="Script timed out after 60s")
        except Exception as e:
            logger.error("X stats script failed", error=str(e))
            return XStatsResult(error=f"Script execution failed: {e}")

        stdout_text = stdout.decode().strip()
        stderr_text = stderr.decode().strip()

        if proc.returncode != 0:
            logger.error(
                "X stats script failed",
                returncode=proc.returncode,
                stderr=stderr_text[:500],
            )
            if not stdout_text:
                return XStatsResult(
                    error=f"Script exited {proc.returncode}: {stderr_text[:200]}"
                )

        if not stdout_text:
            return XStatsResult(error="No output from stats script")

        return XStatsResult(summary=stdout_text)
