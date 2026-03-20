"""Two-phase heartbeat service — cheap Python checks before burning tokens.

Phase 1: Deterministic checks (pending tasks, git, pm2, disk, file changes).
Phase 2: Only invoke Claude if Phase 1 found signals worth reporting.
"""

import asyncio
import hashlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

# Pattern for unchecked markdown tasks
_PENDING_TASK_RE = re.compile(r"^-\s*\[\s*\]\s*(.+)$", re.MULTILINE)


@dataclass
class HeartbeatSignal:
    """A single signal detected during Phase 1."""

    category: str  # pending_tasks, git, process, disk, file_changes
    severity: str  # info, warning, critical
    summary: str
    details: List[str] = field(default_factory=list)


@dataclass
class HeartbeatResult:
    """Aggregated Phase 1 results."""

    signals: List[HeartbeatSignal] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def has_signals(self) -> bool:
        return len(self.signals) > 0

    @property
    def max_severity(self) -> str:
        """Return the highest severity across all signals."""
        if not self.signals:
            return "ok"
        severity_order = {"info": 0, "warning": 1, "critical": 2}
        return max(
            self.signals, key=lambda s: severity_order.get(s.severity, 0)
        ).severity

    def signal_fingerprint(self) -> str:
        """SHA256 fingerprint of current signals for deduplication."""
        parts: List[str] = []
        for sig in sorted(self.signals, key=lambda s: (s.category, s.summary)):
            details_str = "|".join(sorted(sig.details))
            parts.append(f"{sig.category}|{sig.severity}|{sig.summary}|{details_str}")
        raw = "\n".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def build_prompt(self) -> str:
        """Build a focused Claude prompt from detected signals."""
        lines = [
            "Heartbeat detected the following signals. "
            "Summarize what needs attention and recommend actions.\n"
        ]

        for sig in self.signals:
            icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(
                sig.severity, "•"
            )
            lines.append(f"{icon} **{sig.category}** — {sig.summary}")
            for detail in sig.details[:10]:  # Cap details
                lines.append(f"  - {detail}")

        return "\n".join(lines)


class HeartbeatService:
    """Runs cheap deterministic checks before deciding whether to invoke Claude.

    Usage:
        result = await heartbeat.run_checks()
        if result.has_signals:
            # Fire Claude with result.build_prompt()
        else:
            await heartbeat.record_quiet()
    """

    def __init__(
        self,
        working_directory: Path,
        memory_dir: Optional[Path] = None,
        disk_threshold_pct: float = 85.0,
    ) -> None:
        self.working_directory = working_directory
        self.memory_dir = memory_dir or working_directory / ".memory"
        self.disk_threshold_pct = disk_threshold_pct
        self._heartbeat_file = self.memory_dir / "HEARTBEAT.md"
        self._last_heartbeat: Optional[datetime] = self._read_last_heartbeat()
        self._last_fingerprint: Optional[str] = self._read_last_fingerprint()

    async def run_checks(self) -> HeartbeatResult:
        """Run all Phase 1 deterministic checks."""
        checks = await asyncio.gather(
            self._check_pending_tasks(),
            self._check_git_status(),
            self._check_process_health(),
            self._check_disk_space(),
            self._check_file_changes(),
            return_exceptions=True,
        )

        signals: List[HeartbeatSignal] = []
        for result in checks:
            if isinstance(result, Exception):
                logger.warning("Heartbeat check failed", error=str(result))
                continue
            signals.extend(result)

        hb_result = HeartbeatResult(signals=signals)

        logger.info(
            "Heartbeat Phase 1 complete",
            signal_count=len(signals),
            max_severity=hb_result.max_severity,
            has_signals=hb_result.has_signals,
        )
        return hb_result

    async def record_quiet(self) -> None:
        """All-clear: write timestamp to HEARTBEAT.md, skip Claude ($0)."""
        now = datetime.now(UTC)
        self._last_heartbeat = now

        self._heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        self._heartbeat_file.write_text(
            f"# Heartbeat\n\n"
            f"Last check: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"Status: ✅ All quiet — no Claude invocation needed\n"
            f"Signal fingerprint: none\n"
        )
        self._last_fingerprint = None
        logger.info("Heartbeat quiet — no LLM call", timestamp=now.isoformat())

    async def record_signal(self, result: HeartbeatResult) -> None:
        """Signal detected: update HEARTBEAT.md with findings."""
        now = datetime.now(UTC)
        self._last_heartbeat = now
        fingerprint = result.signal_fingerprint()
        self._last_fingerprint = fingerprint

        self._heartbeat_file.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# Heartbeat\n",
            f"Last check: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Status: ⚠️ {len(result.signals)} signal(s) detected — Claude invoked",
            f"Signal fingerprint: {fingerprint}\n",
        ]
        for sig in result.signals:
            lines.append(f"- **{sig.category}**: {sig.summary}")

        self._heartbeat_file.write_text("\n".join(lines) + "\n")

    # ------------------------------------------------------------------
    # Phase 1 checks — all deterministic, no LLM
    # ------------------------------------------------------------------

    async def _check_pending_tasks(self) -> List[HeartbeatSignal]:
        """Parse .memory/ for unchecked tasks (- [ ])."""
        signals: List[HeartbeatSignal] = []
        tasks_found: List[str] = []

        # Scan all MEMORY.md files in .memory/
        if not self.memory_dir.exists():
            return signals

        for memory_file in self.memory_dir.rglob("MEMORY.md"):
            try:
                content = memory_file.read_text()
                matches = _PENDING_TASK_RE.findall(content)
                for task in matches:
                    user_dir = memory_file.parent.name
                    tasks_found.append(f"[{user_dir}] {task.strip()}")
            except Exception as e:
                logger.debug(
                    "Failed to read memory file", path=str(memory_file), error=str(e)
                )

        if tasks_found:
            # Check staleness
            stale_note = ""
            if self._last_heartbeat:
                days = (datetime.now(UTC) - self._last_heartbeat).days
                if days >= 2:
                    stale_note = f" (unchanged for {days}+ days)"

            signals.append(
                HeartbeatSignal(
                    category="pending_tasks",
                    severity="warning" if len(tasks_found) > 3 else "info",
                    summary=f"{len(tasks_found)} pending task(s){stale_note}",
                    details=tasks_found[:10],
                )
            )

        return signals

    async def _check_git_status(self) -> List[HeartbeatSignal]:
        """Check git working tree for uncommitted changes."""
        signals: List[HeartbeatSignal] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--porcelain",
                cwd=str(self.working_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode().strip()

            if output:
                changes = output.splitlines()
                untracked = [l for l in changes if l.startswith("??")]
                modified = [l for l in changes if not l.startswith("??")]

                details = []
                if modified:
                    details.append(f"{len(modified)} modified/staged file(s)")
                if untracked:
                    details.append(f"{len(untracked)} untracked file(s)")
                # Show first few file names
                for line in changes[:8]:
                    details.append(line.strip())

                signals.append(
                    HeartbeatSignal(
                        category="git",
                        severity="info",
                        summary=f"{len(changes)} uncommitted change(s) in working tree",
                        details=details,
                    )
                )

            # Check for recent commits since last heartbeat
            if self._last_heartbeat:
                since = self._last_heartbeat.strftime("%Y-%m-%dT%H:%M:%S")
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "log",
                    f"--since={since}",
                    "--oneline",
                    cwd=str(self.working_directory),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                commits = stdout.decode().strip().splitlines()
                if commits:
                    signals.append(
                        HeartbeatSignal(
                            category="git",
                            severity="info",
                            summary=f"{len(commits)} new commit(s) since last heartbeat",
                            details=commits[:5],
                        )
                    )

        except asyncio.TimeoutError:
            logger.warning("Git status check timed out")
        except FileNotFoundError:
            logger.debug("Git not found, skipping git checks")

        return signals

    async def _check_process_health(self) -> List[HeartbeatSignal]:
        """Check pm2 process status."""
        signals: List[HeartbeatSignal] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                "pm2",
                "jlist",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)

            import json

            processes = json.loads(stdout.decode())
            for p in processes:
                name = p.get("name", "unknown")
                status = p.get("pm2_env", {}).get("status", "unknown")
                restarts = p.get("pm2_env", {}).get("restart_time", 0)
                memory = p.get("monit", {}).get("memory", 0)
                memory_mb = memory / (1024 * 1024)

                if status != "online":
                    signals.append(
                        HeartbeatSignal(
                            category="process",
                            severity="critical",
                            summary=f"Process '{name}' is {status}",
                            details=[
                                f"Restarts: {restarts}",
                                f"Memory: {memory_mb:.0f}MB",
                            ],
                        )
                    )
                elif restarts > 5:
                    signals.append(
                        HeartbeatSignal(
                            category="process",
                            severity="warning",
                            summary=f"Process '{name}' has {restarts} restarts",
                            details=[f"Status: {status}", f"Memory: {memory_mb:.0f}MB"],
                        )
                    )
                elif memory_mb > 500:
                    signals.append(
                        HeartbeatSignal(
                            category="process",
                            severity="warning",
                            summary=f"Process '{name}' using {memory_mb:.0f}MB",
                            details=[f"Status: {status}", f"Restarts: {restarts}"],
                        )
                    )

        except asyncio.TimeoutError:
            logger.warning("pm2 check timed out")
        except FileNotFoundError:
            logger.debug("pm2 not found, skipping process checks")
        except Exception as e:
            logger.debug("pm2 check failed", error=str(e))

        return signals

    async def _check_disk_space(self) -> List[HeartbeatSignal]:
        """Check disk usage against threshold."""
        signals: List[HeartbeatSignal] = []

        try:
            usage = shutil.disk_usage(str(self.working_directory))
            used_pct = (usage.used / usage.total) * 100
            free_gb = usage.free / (1024**3)

            if used_pct >= self.disk_threshold_pct:
                severity = "critical" if used_pct >= 95 else "warning"
                signals.append(
                    HeartbeatSignal(
                        category="disk",
                        severity=severity,
                        summary=f"Disk {used_pct:.1f}% full ({free_gb:.1f}GB free)",
                        details=[
                            f"Total: {usage.total / (1024**3):.1f}GB",
                            f"Used: {usage.used / (1024**3):.1f}GB",
                            f"Threshold: {self.disk_threshold_pct}%",
                        ],
                    )
                )
        except Exception as e:
            logger.warning("Disk check failed", error=str(e))

        return signals

    async def _check_file_changes(self) -> List[HeartbeatSignal]:
        """Check for significant file modifications since last heartbeat."""
        signals: List[HeartbeatSignal] = []

        if not self._last_heartbeat:
            return signals  # No baseline — skip on first run

        cutoff = self._last_heartbeat.timestamp()
        changed_files: List[str] = []

        try:
            src_dir = self.working_directory / "src"
            if src_dir.exists():
                for f in src_dir.rglob("*.py"):
                    try:
                        if f.stat().st_mtime > cutoff:
                            changed_files.append(
                                str(f.relative_to(self.working_directory))
                            )
                    except OSError:
                        continue

            if changed_files:
                signals.append(
                    HeartbeatSignal(
                        category="file_changes",
                        severity="info",
                        summary=f"{len(changed_files)} source file(s) modified since last heartbeat",
                        details=changed_files[:10],
                    )
                )
        except Exception as e:
            logger.warning("File change check failed", error=str(e))

        return signals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_last_fingerprint(self) -> Optional[str]:
        """Read last signal fingerprint from HEARTBEAT.md."""
        if not self._heartbeat_file.exists():
            return None

        try:
            content = self._heartbeat_file.read_text()
            match = re.search(r"Signal fingerprint:\s*(\S+)", content)
            if match:
                value = match.group(1)
                return None if value == "none" else value
        except Exception as e:
            logger.debug("Failed to parse fingerprint from HEARTBEAT.md", error=str(e))

        return None

    def is_duplicate_signal(self, result: HeartbeatResult) -> bool:
        """Check if the current signals match the last recorded fingerprint."""
        if self._last_fingerprint is None:
            return False
        return result.signal_fingerprint() == self._last_fingerprint

    def _read_last_heartbeat(self) -> Optional[datetime]:
        """Read last heartbeat timestamp from HEARTBEAT.md."""
        if not self._heartbeat_file.exists():
            return None

        try:
            content = self._heartbeat_file.read_text()
            # Parse "Last check: 2026-03-19 14:30:00 UTC"
            match = re.search(
                r"Last check:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC",
                content,
            )
            if match:
                return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
        except Exception as e:
            logger.debug("Failed to parse HEARTBEAT.md", error=str(e))

        return None
