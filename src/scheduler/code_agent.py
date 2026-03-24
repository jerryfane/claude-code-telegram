"""Code agent service — spawns real claude CLI subprocesses for delegated coding.

Phobos or Jerry triggers /code, which launches a claude CLI process with
stream-json I/O. Output streams to a Telegram reply thread in real-time.
Jerry can reply in the thread to steer the sub-agent, or /code kill to stop it.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

import structlog

logger = structlog.get_logger()

# Defaults
DEFAULT_MAX_BUDGET = 0.50  # USD per invocation
DEFAULT_MAX_DURATION = 300  # seconds
DEFAULT_CLI_PATH = "/home/pi/.local/bin/claude"


class CodeAgentSession:
    """A running claude CLI subprocess with bidirectional JSON streaming."""

    def __init__(
        self,
        task: str,
        working_directory: Path,
        cli_path: str,
        output_callback: Callable[[Dict[str, Any]], Coroutine],
        permission_mode: str = "plan",
        max_budget: float = DEFAULT_MAX_BUDGET,
        max_duration: int = DEFAULT_MAX_DURATION,
        model: Optional[str] = None,
    ) -> None:
        self.task = task
        self.working_directory = working_directory
        self.cli_path = cli_path
        self.output_callback = output_callback
        self.permission_mode = permission_mode
        self.max_budget = max_budget
        self.max_duration = max_duration
        self.model = model

        self.session_id = str(uuid.uuid4())
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.status = "pending"  # pending, running, completed, killed, failed
        self.message_thread_id: Optional[int] = None  # Forum topic thread ID
        self.total_cost: float = 0
        self.num_turns: int = 0
        self.result_text: str = ""

        self._process: Optional[asyncio.subprocess.Process] = None
        self._read_task: Optional[asyncio.Task] = None
        self._timeout_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Spawn the claude CLI with stream-json I/O."""
        system_prompt = (
            f"You are a code agent spawned to execute a specific task.\n"
            f"Your task: {self.task}\n\n"
            f"Constraints:\n"
            f"- All file operations must stay within {self.working_directory}\n"
            f"- Complete the task, then stop. Do not ask for clarification.\n"
            f"- Be concise in your reasoning. The orchestrator is watching.\n"
            f"- If you hit an error, try to fix it. After 2 failed attempts, report and stop.\n"
        )

        if self.permission_mode == "acceptEdits":
            system_prompt += (
                f"\nIMPORTANT: You have full edit permissions. "
                f"Do NOT enter plan mode. Do NOT call EnterPlanMode or ExitPlanMode. "
                f"Read what you need, then make the changes directly.\n"
            )

        cmd = [
            self.cli_path,
            "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-mode", self.permission_mode,
            "--max-budget-usd", str(self.max_budget),
            "--system-prompt", system_prompt,
            "--setting-sources", "",
            # Task sent via stdin, not as CLI arg (required for stream-json input)
        ]

        if self.model:
            cmd.extend(["--model", self.model])

        logger.info(
            "Spawning code agent",
            session_id=self.session_id,
            task=self.task[:80],
            mode=self.permission_mode,
            budget=self.max_budget,
        )

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.working_directory),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self.started_at = datetime.now(UTC)
        self.status = "running"

        # Start background readers
        self._read_task = asyncio.create_task(self._read_output_loop())
        self._timeout_task = asyncio.create_task(self._timeout_watchdog())

        # Send the task as the first user message
        await self.send_message(self.task)

    async def send_message(self, text: str) -> bool:
        """Send a user message to the sub-agent via stdin (stream-json format)."""
        if not self._process or self._process.stdin is None:
            return False
        if self.status != "running":
            return False

        msg = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }) + "\n"
        try:
            self._process.stdin.write(msg.encode())
            await self._process.stdin.drain()
            logger.info("Sent steering message to code agent", text=text[:80])
            return True
        except Exception as e:
            logger.warning("Failed to send to code agent stdin", error=str(e))
            return False

    async def kill(self) -> None:
        """Terminate the subprocess."""
        if self._process and self.status == "running":
            self.status = "killed"
            self.finished_at = datetime.now(UTC)
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            logger.info("Code agent killed", session_id=self.session_id)

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()

    async def _read_output_loop(self) -> None:
        """Read stream-json lines from stdout and forward to callback."""
        assert self._process and self._process.stdout

        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break  # EOF — process exited

                line_str = line.decode().strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                # Extract result info (but keep session alive for multi-turn)
                if msg_type == "result":
                    self.total_cost = data.get("total_cost_usd", 0)
                    self.num_turns = data.get("num_turns", 0)
                    self.result_text = data.get("result", "")[:2000]
                    # Don't mark completed — process stays alive for follow-up messages
                    # Session ends when: process exits (EOF), kill(), or timeout

                # Forward to callback (Telegram delivery)
                try:
                    await self.output_callback(data)
                except Exception:
                    logger.debug("Output callback error", exc_info=True)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Code agent output reader failed")
        finally:
            # Ensure status is set
            if self.status == "running":
                self.status = "completed"
                self.finished_at = datetime.now(UTC)
            if self._timeout_task and not self._timeout_task.done():
                self._timeout_task.cancel()

    async def _timeout_watchdog(self) -> None:
        """Kill the process if it exceeds max duration."""
        try:
            await asyncio.sleep(self.max_duration)
            if self.status == "running":
                logger.warning(
                    "Code agent timed out",
                    session_id=self.session_id,
                    max_duration=self.max_duration,
                )
                self.status = "failed"
                await self.output_callback({
                    "type": "system",
                    "subtype": "timeout",
                    "message": f"Code agent timed out after {self.max_duration}s",
                })
                await self.kill()
        except asyncio.CancelledError:
            pass

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.finished_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()


class CodeAgentService:
    """Manages active code agent sessions. One session per chat."""

    PENDING_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "pending_code_tasks.json"

    def __init__(
        self,
        working_directory: Path,
        cli_path: str = DEFAULT_CLI_PATH,
        max_budget: float = DEFAULT_MAX_BUDGET,
        max_duration: int = DEFAULT_MAX_DURATION,
    ) -> None:
        self.working_directory = working_directory
        self.cli_path = cli_path
        self.max_budget = max_budget
        self.max_duration = max_duration
        self.active_sessions: Dict[int, CodeAgentSession] = {}
        self._bot: Any = None  # Telegram Bot instance, set via set_bot()
        self._is_forum: Dict[int, bool] = {}  # chat_id -> is_forum

    def set_bot(self, bot: Any) -> None:
        """Store Telegram bot reference for spawning tasks from pending file."""
        self._bot = bot

    async def spawn(
        self,
        task: str,
        chat_id: int,
        output_callback: Callable[[Dict[str, Any]], Coroutine],
        permission_mode: str = "plan",
        model: Optional[str] = None,
    ) -> CodeAgentSession:
        """Create and start a new code agent session."""
        # Kill any existing session for this chat
        if chat_id in self.active_sessions:
            old = self.active_sessions[chat_id]
            if old.status == "running":
                await old.kill()

        session = CodeAgentSession(
            task=task,
            working_directory=self.working_directory,
            cli_path=self.cli_path,
            output_callback=output_callback,
            permission_mode=permission_mode,
            max_budget=self.max_budget,
            max_duration=self.max_duration,
            model=model,
        )
        self.active_sessions[chat_id] = session
        await session.start()
        return session

    def get_session(self, chat_id: int) -> Optional[CodeAgentSession]:
        """Get the active session for a chat."""
        session = self.active_sessions.get(chat_id)
        if session and session.status == "running":
            return session
        return None

    async def kill_session(self, chat_id: int) -> bool:
        """Kill the active session for a chat."""
        session = self.active_sessions.get(chat_id)
        if session and session.status == "running":
            await session.kill()
            return True

    async def process_pending_tasks(self, override_chat_id: Optional[int] = None) -> int:
        """Pick up pending code tasks written by scripts/code_task.py.

        Called as fire-and-forget after each Claude response.
        override_chat_id: use this chat instead of what the script wrote
        (auto-detects group vs private based on where the conversation is).
        Returns count of tasks spawned.
        """
        if not self._bot or not self.PENDING_FILE.exists():
            return 0

        try:
            pending = json.loads(self.PENDING_FILE.read_text())
        except Exception:
            return 0

        if not pending:
            return 0

        spawned = 0
        for task_info in pending:
            try:
                chat_id = override_chat_id or task_info.get("chat_id", 0)
                task = task_info.get("task", "")
                mode = task_info.get("mode", "build")

                if not chat_id or not task:
                    continue

                # Skip if session already running for this chat
                if self.get_session(chat_id):
                    logger.warning("Code agent already running for chat", chat_id=chat_id)
                    continue

                permission_mode = "acceptEdits" if mode == "build" else "plan"

                # Try to create forum topic
                message_thread_id = None
                try:
                    chat = await self._bot.get_chat(chat_id)
                    if getattr(chat, "is_forum", False):
                        topic = await self._bot.create_forum_topic(
                            chat_id=chat_id,
                            name=f"🤖 Code: {task[:40]}",
                        )
                        message_thread_id = topic.message_thread_id
                except Exception:
                    logger.debug("Could not create forum topic for pending task")

                # Send initial message
                mode_label = "build" if permission_mode == "acceptEdits" else "plan"
                send_kwargs: Dict[str, Any] = {"parse_mode": "HTML"}
                if message_thread_id:
                    send_kwargs["message_thread_id"] = message_thread_id

                initial_msg = await self._bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🤖 <b>Code Agent</b> ({mode_label} mode)\n"
                        f"Task: {task[:200]}\n\n"
                        f"<i>Spawned by Phobos. Reply to steer. /code kill to stop.</i>"
                    ),
                    **send_kwargs,
                )

                # Build output callback
                import time as _time
                activity_log: list = []
                last_edit_time = [0.0]
                bot = self._bot

                async def _make_callback(
                    _chat_id: int, _thread_id: Optional[int],
                    _initial_msg: Any, _activity: list, _last_edit: list,
                    _mode_label: str, _task: str,
                ) -> Callable:
                    async def _cb(data: dict) -> None:
                        msg_type = data.get("type", "")

                        if msg_type == "assistant":
                            for block in data.get("message", {}).get("content", []):
                                if block.get("type") == "tool_use":
                                    name = block.get("name", "?")
                                    inp = block.get("input", {})
                                    if name == "Bash":
                                        _activity.append(f"⚡ Bash: {inp.get('command','?')[:80]}")
                                    elif name in ("Read", "Edit", "Write", "Glob", "Grep"):
                                        _activity.append(f"🔧 {name} {inp.get('file_path', inp.get('pattern','?'))[:60]}")
                                    else:
                                        _activity.append(f"🔧 {name}")
                                elif block.get("type") == "text":
                                    t = block.get("text", "").strip()
                                    if t:
                                        _activity.append(t[:300])

                        elif msg_type == "result":
                            result = data.get("result", "")
                            cost = data.get("total_cost_usd", 0)
                            turns = data.get("num_turns", 0)
                            is_error = data.get("is_error", False)
                            icon = "❌" if is_error else "✅"
                            header = f"{icon} <b>Done</b> — ${cost:.4f}, {turns} turns\n\n"
                            full = header + result
                            kw: Dict[str, Any] = {}
                            if _thread_id:
                                kw["message_thread_id"] = _thread_id
                            for i in range(0, len(full), 4000):
                                chunk = full[i:i+4000]
                                parse = "HTML" if i == 0 else None
                                try:
                                    await bot.send_message(chat_id=_chat_id, text=chunk, parse_mode=parse, **kw)
                                except Exception:
                                    pass
                            return

                        else:
                            return

                        now = _time.time()
                        if now - _last_edit[0] < 2.0:
                            return
                        _last_edit[0] = now

                        if _thread_id:
                            recent = _activity[-3:]
                            text = "\n".join(recent)
                            if text.strip():
                                try:
                                    await bot.send_message(chat_id=_chat_id, text=text[:4000], message_thread_id=_thread_id)
                                except Exception:
                                    pass
                        else:
                            recent = _activity[-10:]
                            status = f"🤖 <b>Code Agent</b> ({_mode_label})\nTask: {_task[:100]}\n\n" + "\n".join(recent)
                            try:
                                await bot.edit_message_text(text=status[:4000], chat_id=_chat_id, message_id=_initial_msg.message_id, parse_mode="HTML")
                            except Exception:
                                pass

                    return _cb

                callback = await _make_callback(
                    chat_id, message_thread_id, initial_msg,
                    activity_log, last_edit_time, mode_label, task,
                )

                session = await self.spawn(
                    task=task,
                    chat_id=chat_id,
                    output_callback=callback,
                    permission_mode=permission_mode,
                )
                session.message_thread_id = message_thread_id
                spawned += 1
                logger.info("Spawned code agent from pending task", task=task[:60], chat_id=chat_id)

            except Exception:
                logger.exception("Failed to process pending code task")

        # Clear pending file
        try:
            self.PENDING_FILE.write_text("[]\n")
        except Exception:
            pass

        return spawned
        return False
