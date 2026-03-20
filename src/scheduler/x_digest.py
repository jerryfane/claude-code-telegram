"""X/Twitter daily digest service — shells out to scripts/x_digest.py.

Follows the same subprocess pattern as HeartbeatService: run an external
script, parse structured output, and provide results for Claude summarization.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Default paths relative to working directory
_DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "x_digest.py"
_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "x_digest.json"


@dataclass
class TopicResult:
    """Search results for a single topic."""

    topic: str
    query: str
    tweets: List[Dict]
    error: Optional[str] = None


@dataclass
class XDigestResult:
    """Aggregated digest results from the search script."""

    topics: List[TopicResult] = field(default_factory=list)
    searched_at: Optional[str] = None
    error: Optional[str] = None

    @property
    def has_results(self) -> bool:
        """True if any topic returned tweets."""
        return any(len(t.tweets) > 0 for t in self.topics)

    @property
    def total_tweets(self) -> int:
        return sum(len(t.tweets) for t in self.topics)

    def build_prompt(self) -> str:
        """Build a Claude prompt from the search results."""
        lines = [
            "Here are today's tweets from X/Twitter on topics I follow. "
            "Summarize the key discussions, notable takes, and anything worth "
            "paying attention to. Group by topic and highlight the most "
            "interesting or viral tweets.\n"
        ]

        for topic in self.topics:
            lines.append(f"## {topic.topic}")
            if topic.error:
                lines.append(f"(Search failed: {topic.error})")
                continue
            if not topic.tweets:
                lines.append("(No tweets found)")
                continue

            for tweet in topic.tweets:
                user = tweet.get("user", {})
                name = user.get("name", "?")
                handle = user.get("screen_name", "?")
                text = tweet.get("text", "")
                likes = tweet.get("favorite_count", 0)
                rts = tweet.get("retweet_count", 0)
                lines.append(
                    f"- @{handle} ({name}): {text}\n"
                    f"  [{likes} likes, {rts} RTs]"
                )

            lines.append("")

        return "\n".join(lines)


class XDigestService:
    """Runs the X/Twitter digest search script and parses results.

    Usage:
        result = await service.run()
        if result.has_results:
            prompt = result.build_prompt()
            # Send to Claude for summarization
    """

    def __init__(
        self,
        working_directory: Path,
        script_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self.working_directory = working_directory
        self.script_path = script_path or _DEFAULT_SCRIPT
        self.config_path = config_path or _DEFAULT_CONFIG

    async def run(self) -> XDigestResult:
        """Execute the search script and parse JSON output."""
        if not self.script_path.exists():
            return XDigestResult(
                error=f"Script not found: {self.script_path}"
            )

        if not self.config_path.exists():
            return XDigestResult(
                error=f"Config not found: {self.config_path}"
            )

        logger.info(
            "Running X digest search",
            script=str(self.script_path),
            config=str(self.config_path),
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self.script_path),
                "--config",
                str(self.config_path),
                cwd=str(self.working_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
        except asyncio.TimeoutError:
            logger.error("X digest script timed out")
            return XDigestResult(error="Script timed out after 120s")
        except Exception as e:
            logger.error("X digest script execution failed", error=str(e))
            return XDigestResult(error=f"Script execution failed: {e}")

        stdout_text = stdout.decode().strip()
        stderr_text = stderr.decode().strip()

        if proc.returncode != 0:
            logger.error(
                "X digest script failed",
                returncode=proc.returncode,
                stderr=stderr_text[:500],
            )
            # Try to parse stdout even on failure — script may have partial results
            if not stdout_text:
                return XDigestResult(
                    error=f"Script exited {proc.returncode}: {stderr_text[:200]}"
                )

        # Parse JSON output
        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError as e:
            logger.error(
                "X digest script output is not valid JSON",
                error=str(e),
                output=stdout_text[:200],
            )
            return XDigestResult(error=f"Invalid JSON output: {e}")

        return self._parse_result(data)

    def _parse_result(self, data: dict) -> XDigestResult:
        """Parse the JSON output from the search script."""
        topics = []
        for t in data.get("topics", []):
            topics.append(
                TopicResult(
                    topic=t.get("topic", "?"),
                    query=t.get("query", ""),
                    tweets=t.get("tweets", []),
                    error=t.get("error"),
                )
            )

        return XDigestResult(
            topics=topics,
            searched_at=data.get("searched_at"),
            error=data.get("error"),
        )
