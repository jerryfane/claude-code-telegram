"""Moltbook post performance tracker — shells out to scripts/moltbook_stats.py.

Polls the Moltbook API for post/comment stats, auto-discovers new posts,
computes deltas, and provides formatted summaries for Telegram display.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

_DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "moltbook_stats.py"
_DEFAULT_CREDENTIALS = Path(__file__).resolve().parent.parent.parent / "data" / "moltbook_credentials.json"
_DEFAULT_TRACKER = (
    Path(__file__).resolve().parent.parent.parent
    / ".memory" / "users" / "13218410" / "memory" / "projects" / "moltbook_posts.json"
)


@dataclass
class MoltbookStatsResult:
    """Parsed results from the stats polling script."""

    account: Dict = field(default_factory=dict)
    posts: List[Dict] = field(default_factory=list)
    comments_count: int = 0
    polled_at: Optional[str] = None
    error: Optional[str] = None

    @property
    def has_results(self) -> bool:
        return len(self.posts) > 0

    @property
    def total_upvotes(self) -> int:
        return sum(p.get("upvotes", 0) for p in self.posts)

    def build_summary(self) -> str:
        """Build a formatted stats summary for Telegram (HTML)."""
        lines = []

        # Account header
        karma = self.account.get("karma", "?")
        followers = self.account.get("followers", "?")
        posts_count = self.account.get("posts_count", "?")
        comments_count = self.account.get("comments_count", "?")
        lines.append(
            f"<b>@phobosintern</b> — "
            f"Karma: {karma} | "
            f"Followers: {followers} | "
            f"{posts_count} posts, {comments_count} comments"
        )
        lines.append("")

        # Posts table
        for p in self.posts:
            title = p.get("title", "?")[:55]
            upvotes = p.get("upvotes", 0)
            delta_up = p.get("delta_upvotes", 0)
            comments = p.get("comment_count", 0)
            delta_c = p.get("delta_comments", 0)

            # Age
            created = p.get("created_at", "")
            age_str = _format_age(created)

            # Delta indicators
            up_delta = f" (+{delta_up})" if delta_up > 0 else ""
            c_delta = f" (+{delta_c})" if delta_c > 0 else ""

            lines.append(
                f"• <b>{title}</b>\n"
                f"  {age_str} | ⬆ {upvotes}{up_delta} | 💬 {comments}{c_delta}"
            )

        return "\n".join(lines)


def _format_age(iso_str: str) -> str:
    """Convert ISO timestamp to human-readable age."""
    if not iso_str:
        return "?"
    try:
        created = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m"
        if hours < 24:
            return f"{int(hours)}h"
        return f"{int(hours / 24)}d"
    except Exception:
        return "?"


class MoltbookStatsService:
    """Runs the Moltbook stats polling script and parses results."""

    def __init__(
        self,
        working_directory: Path,
        script_path: Optional[Path] = None,
        credentials_path: Optional[Path] = None,
        tracker_path: Optional[Path] = None,
    ) -> None:
        self.working_directory = working_directory
        self.script_path = script_path or _DEFAULT_SCRIPT
        self.credentials_path = credentials_path or _DEFAULT_CREDENTIALS
        self.tracker_path = tracker_path or _DEFAULT_TRACKER

    async def run(self) -> MoltbookStatsResult:
        """Execute the stats script and parse JSON output."""
        if not self.script_path.exists():
            return MoltbookStatsResult(error=f"Script not found: {self.script_path}")

        if not self.credentials_path.exists():
            return MoltbookStatsResult(error=f"Credentials not found: {self.credentials_path}")

        logger.info("Running Moltbook stats poll", script=str(self.script_path))

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self.script_path),
                "--credentials",
                str(self.credentials_path),
                "--tracker",
                str(self.tracker_path),
                cwd=str(self.working_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=90
            )
        except asyncio.TimeoutError:
            logger.error("Moltbook stats script timed out")
            return MoltbookStatsResult(error="Script timed out after 90s")
        except Exception as e:
            logger.error("Moltbook stats script failed", error=str(e))
            return MoltbookStatsResult(error=f"Script execution failed: {e}")

        stdout_text = stdout.decode().strip()
        stderr_text = stderr.decode().strip()

        if proc.returncode != 0:
            logger.error(
                "Moltbook stats script failed",
                returncode=proc.returncode,
                stderr=stderr_text[:500],
            )
            if not stdout_text:
                return MoltbookStatsResult(
                    error=f"Script exited {proc.returncode}: {stderr_text[:200]}"
                )

        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON from moltbook_stats", error=str(e))
            return MoltbookStatsResult(error=f"Invalid JSON: {e}")

        return MoltbookStatsResult(
            account=data.get("account", {}),
            posts=data.get("posts", []),
            comments_count=data.get("comments_count", 0),
            polled_at=data.get("polled_at"),
            error=data.get("error"),
        )

    async def read_tracker(self) -> MoltbookStatsResult:
        """Read the tracker file directly without polling the API.

        Used by /moltbook stats for instant display.
        """
        if not self.tracker_path.exists():
            return MoltbookStatsResult(error="No tracker data yet. Run /cron test moltbook_stats first.")

        try:
            data = json.loads(self.tracker_path.read_text())
        except Exception as e:
            return MoltbookStatsResult(error=f"Failed to read tracker: {e}")

        # Build summary posts with deltas
        posts = []
        for p in data.get("posts", []):
            posts.append({
                "id": p.get("id", ""),
                "title": p.get("title", "")[:60],
                "created_at": p.get("created_at", ""),
                "upvotes": p.get("upvotes", 0),
                "delta_upvotes": p.get("upvotes", 0) - p.get("prev_upvotes", 0),
                "comment_count": p.get("comment_count", 0),
                "delta_comments": p.get("comment_count", 0) - p.get("prev_comment_count", 0),
                "score": p.get("score", 0),
            })

        return MoltbookStatsResult(
            account=data.get("account", {}),
            posts=posts,
            comments_count=len(data.get("comments", [])),
            polled_at=data.get("last_full_poll"),
        )
