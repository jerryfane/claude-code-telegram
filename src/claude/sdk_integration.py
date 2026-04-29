"""Claude Code Python SDK integration."""

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import StreamEvent

from ..config.settings import Settings
from ..events.bus import EventBus
from ..events.types import DashboardStreamEvent
from ..memory import make_memory_server
from ..security.validators import SecurityValidator
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)
from .monitor import _is_claude_internal_path, check_bash_directory_boundary

logger = structlog.get_logger()

# Fallback message when Claude produces no text but did use tools.
TASK_COMPLETED_MSG = "✅ Task completed. Tools used: {tools_summary}"
TURN_LIMIT_NOTE = (
    "\n\nReached the configured turn limit ({turn_limit}). "
    "Send /continue to resume."
)
TURN_LIMIT_RECOVERY_PROMPT = """
You reached the configured tool/turn limit for the previous request.

Do not call any tools. Provide a concise final response for the user that
summarizes:
1. What you found or changed.
2. What remains unresolved.
3. The next concrete step if they want to continue.
""".strip()


# Anthropic's auto-injected memory protocol (used when memory_20250818 ships
# via the raw API). The Agent SDK doesn't auto-inject this, so we append it to
# the system prompt whenever we wire the in-process memory server.
MEMORY_PROTOCOL_PROMPT = """
# Memory Protocol

IMPORTANT: ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE FOR \
A NON-TRIVIAL REQUEST.

You have a memory store at /memories/, accessed via tools named \
mcp__memory__view, mcp__memory__create, mcp__memory__str_replace, \
mcp__memory__insert, mcp__memory__delete, and mcp__memory__rename.

1. Use `mcp__memory__view` with path /memories to check for earlier progress.
2. Work on the task; record load-bearing facts (preferences, decisions, \
project context) in memory as you go.
3. Keep /memories/MEMORY.md as a short index pointing to deeper files in \
/memories/people/, /memories/projects/, /memories/topics/, /memories/decisions/.

ASSUME INTERRUPTION: Your context window or session may reset at any moment, \
so anything not recorded in /memories/ is lost.
""".strip()


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False
    hit_turn_limit: bool = False
    turn_limit: Optional[int] = None
    turn_limit_recovery_attempted: bool = False
    turn_limit_recovery_succeeded: bool = False


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    type: str  # 'assistant', 'user', 'system', 'result', 'stream_delta'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None

    def get_tool_names(self) -> List[str]:
        """Return tool names from the stream payload."""
        names: List[str] = []

        if self.tool_calls:
            for tool_call in self.tool_calls:
                name = tool_call.get("name") if isinstance(tool_call, dict) else None
                if isinstance(name, str) and name:
                    names.append(name)

        if self.metadata:
            tool_name = self.metadata.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                names.append(tool_name)

            metadata_tools = self.metadata.get("tools")
            if isinstance(metadata_tools, list):
                for tool in metadata_tools:
                    if isinstance(tool, dict):
                        name = tool.get("name")
                    elif isinstance(tool, str):
                        name = tool
                    else:
                        name = None

                    if isinstance(name, str) and name:
                        names.append(name)

        # Preserve insertion order while de-duplicating.
        return list(dict.fromkeys(names))

    def is_error(self) -> bool:
        """Check whether this stream update represents an error."""
        if self.type == "error":
            return True

        if self.metadata:
            if self.metadata.get("is_error") is True:
                return True
            status = self.metadata.get("status")
            if isinstance(status, str) and status.lower() == "error":
                return True
            error_val = self.metadata.get("error")
            if isinstance(error_val, str) and error_val:
                return True
            error_msg_val = self.metadata.get("error_message")
            if isinstance(error_msg_val, str) and error_msg_val:
                return True

        if self.progress:
            status = self.progress.get("status")
            if isinstance(status, str) and status.lower() == "error":
                return True

        return False

    def get_error_message(self) -> str:
        """Get the best available error message from the stream payload."""
        if self.metadata:
            for key in ("error_message", "error", "message"):
                value = self.metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value

        if isinstance(self.content, str) and self.content.strip():
            return self.content

        if self.progress:
            value = self.progress.get("error")
            if isinstance(value, str) and value.strip():
                return value

        return "Unknown error"

    def get_progress_percentage(self) -> Optional[int]:
        """Extract progress percentage if present."""

        def _to_int(value: Any) -> Optional[int]:
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.strip():
                try:
                    return int(float(value))
                except ValueError:
                    return None
            return None

        if self.progress:
            for key in ("percentage", "percent", "progress"):
                percentage = _to_int(self.progress.get(key))
                if percentage is not None:
                    return max(0, min(100, percentage))

            step = _to_int(self.progress.get("step"))
            total_steps = _to_int(self.progress.get("total_steps"))
            if step is not None and total_steps and total_steps > 0:
                return max(0, min(100, int((step / total_steps) * 100)))

        if self.metadata:
            percentage = _to_int(self.metadata.get("progress_percentage"))
            if percentage is not None:
                return max(0, min(100, percentage))

        return None


def _make_can_use_tool_callback(
    security_validator: SecurityValidator,
    working_directory: Path,
    approved_directory: Path,
) -> Any:
    """Create a can_use_tool callback for SDK-level tool permission validation.

    The callback validates file path boundaries and bash directory boundaries
    *before* the SDK executes the tool, providing preventive security enforcement.
    """
    _FILE_TOOLS = {"Write", "Edit", "Read", "create_file", "edit_file", "read_file"}
    _BASH_TOOLS = {"Bash", "bash", "shell"}

    async def can_use_tool(
        tool_name: str,
        tool_input: Dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        # File path validation
        if tool_name in _FILE_TOOLS:
            file_path = tool_input.get("file_path") or tool_input.get("path")
            if file_path:
                # Allow Claude Code internal paths (~/.claude/plans/, etc.)
                if _is_claude_internal_path(file_path):
                    return PermissionResultAllow()

                valid, _resolved, error = security_validator.validate_path(
                    file_path, working_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied file operation",
                        tool_name=tool_name,
                        file_path=file_path,
                        error=error,
                    )
                    return PermissionResultDeny(message=error or "Invalid file path")

        # Bash directory boundary validation
        if tool_name in _BASH_TOOLS:
            command = tool_input.get("command", "")
            if command:
                valid, error = check_bash_directory_boundary(
                    command, working_directory, approved_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied bash command",
                        tool_name=tool_name,
                        command=command,
                        error=error,
                    )
                    return PermissionResultDeny(
                        message=error or "Bash directory boundary violation"
                    )

        return PermissionResultAllow()

    return can_use_tool


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
        event_bus: Optional[EventBus] = None,
    ):
        """Initialize SDK manager with configuration."""
        self.config = config
        self.security_validator = security_validator
        self.event_bus = event_bus

        # Set up environment for Claude Code SDK if API key is provided
        # If no API key is provided, the SDK will use existing CLI authentication
        if config.anthropic_api_key_str:
            os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            logger.info("Using provided API key for Claude SDK authentication")
        else:
            logger.info("No API key provided, using existing Claude CLI authentication")

    def _resolve_memory_file(
        self,
        memory_dir: Path,
        user_id: Optional[int],
        filename: str,
    ) -> Optional[str]:
        """Resolve a single memory file with user -> default fallback."""
        if user_id is not None:
            user_path = memory_dir / "users" / str(user_id) / filename
            if user_path.is_file():
                try:
                    return user_path.read_text(encoding="utf-8")
                except OSError:
                    logger.warning(
                        "Failed to read user memory file",
                        path=str(user_path),
                        user_id=user_id,
                    )

        default_path = memory_dir / "default" / filename
        if default_path.is_file():
            try:
                return default_path.read_text(encoding="utf-8")
            except OSError:
                logger.warning(
                    "Failed to read default memory file",
                    path=str(default_path),
                )

        return None

    def _build_memory_prompt(self, user_id: Optional[int]) -> str:
        """Load per-user memory files and return them as a system prompt section.

        Resolution order for each file:
        1. .memory/users/{user_id}/{file} -- per-user override
        2. .memory/default/{file} -- project-level default
        3. (skip) -- no injection for that file
        """
        memory_dir = self.config.resolved_memory_dir
        if not memory_dir:
            return ""

        memory_files = ["SOUL.md", "USER.md", "MEMORY.md"]

        sections: List[str] = []
        for filename in memory_files:
            content = self._resolve_memory_file(memory_dir, user_id, filename)
            if content:
                label = filename.replace(".md", "").upper()
                sections.append(f"## {label}\n\n{content}")

        if not sections:
            return ""

        return "# Assistant Memory\n\n" + "\n\n---\n\n".join(sections)

    def _is_retryable_error(self, exc: BaseException) -> bool:
        """Return True for transient errors that warrant a retry.
        asyncio.TimeoutError is intentional (user-configured timeout) — not retried.
        Only non-MCP CLIConnectionError is considered transient.
        """
        if isinstance(exc, CLIConnectionError):
            msg = str(exc).lower()
            return "mcp" not in msg  # "server" alone is too broad
        return False

    async def _collect_client_messages(
        self,
        options: ClaudeAgentOptions,
        prompt: str,
        messages: List[Message],
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        session_id: Optional[str] = None,
        user_id: Optional[int] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run one SDK query and append parsed messages to ``messages``."""
        client = ClaudeSDKClient(options)
        try:
            await client.connect()

            if images:
                content_blocks: List[Dict[str, Any]] = []
                for img in images:
                    media_type = img.get("media_type", "image/png")
                    content_blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": img["data"],
                            },
                        }
                    )
                content_blocks.append({"type": "text", "text": prompt})

                multimodal_msg = {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": content_blocks,
                    },
                }

                async def _multimodal_prompt() -> AsyncIterator[Dict[str, Any]]:
                    yield multimodal_msg

                await client.query(_multimodal_prompt())
            else:
                await client.query(prompt)

            async for raw_data in client._query.receive_messages():
                try:
                    message = parse_message(raw_data)
                except MessageParseError as e:
                    logger.debug(
                        "Skipping unparseable message",
                        error=str(e),
                    )
                    continue

                messages.append(message)

                if isinstance(message, ResultMessage):
                    break

                if stream_callback:
                    try:
                        await self._handle_stream_message(
                            message,
                            stream_callback,
                            session_id=session_id,
                            user_id=user_id,
                        )
                    except Exception as callback_error:
                        logger.warning(
                            "Stream callback failed",
                            error=str(callback_error),
                            error_type=type(callback_error).__name__,
                        )
        finally:
            await client.disconnect()

    def _assistant_has_tool_use(self, message: Message) -> bool:
        if not isinstance(message, AssistantMessage):
            return False
        content = getattr(message, "content", [])
        return isinstance(content, list) and any(
            isinstance(block, ToolUseBlock) for block in content
        )

    def _extract_assistant_text(self, message: Message) -> str:
        if not isinstance(message, AssistantMessage):
            return ""
        content = getattr(message, "content", [])
        text_parts: List[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ThinkingBlock):
                    text_parts.append(block.thinking)
                elif hasattr(block, "text"):
                    text_parts.append(str(block.text))
        elif content:
            text_parts.append(str(content))
        return "\n".join(part for part in text_parts if part).strip()

    def _last_assistant_message(self, messages: List[Message]) -> Optional[Message]:
        for message in reversed(messages):
            if isinstance(message, AssistantMessage):
                return message
        return None

    def _looks_like_incomplete_transition(self, content: str) -> bool:
        """Heuristic for responses that end while promising another action."""
        text = (content or "").strip()
        if not text:
            return True
        lower = text.lower()
        incomplete_suffixes = (
            "let me check",
            "let me check:",
            "let me inspect",
            "let me inspect:",
            "let me look",
            "let me look:",
            "i'll check",
            "i'll check:",
            "i will check",
            "i will check:",
            "checking",
            "checking:",
        )
        if lower.endswith(incomplete_suffixes):
            return True
        return lower.endswith(":") and any(
            phrase in lower[-160:]
            for phrase in (
                "let me",
                "i'll",
                "i will",
                "next",
                "now",
                "check",
                "inspect",
                "look",
            )
        )

    def _should_attempt_turn_limit_recovery(
        self,
        *,
        result_num_turns: Optional[int],
        content: str,
        messages: List[Message],
        interrupted: bool,
    ) -> bool:
        """Return True when the SDK likely stopped because max_turns was hit."""
        if interrupted:
            return False
        turn_limit = self.config.claude_max_turns
        if result_num_turns is None or result_num_turns < turn_limit:
            return False

        last_assistant = self._last_assistant_message(messages)
        if last_assistant is not None and self._assistant_has_tool_use(last_assistant):
            return True

        if not content.strip():
            return True

        if content.startswith("✅ Task completed. Tools used:"):
            return True

        return self._looks_like_incomplete_transition(content)

    async def _attempt_turn_limit_recovery(
        self,
        base_options: ClaudeAgentOptions,
        session_id: str,
    ) -> tuple[str, float, int, Optional[str]]:
        """Ask Claude for one no-tools final summary after max_turn exhaustion."""

        def _make_recovery_options(
            *,
            allowed_tools: Optional[List[str]],
            disallowed_tools: Optional[List[str]],
        ) -> ClaudeAgentOptions:
            recovery_options = ClaudeAgentOptions(
                max_turns=1,
                model=getattr(base_options, "model", None),
                max_budget_usd=getattr(base_options, "max_budget_usd", None),
                cwd=getattr(base_options, "cwd", None),
                allowed_tools=allowed_tools,  # type: ignore[arg-type]
                disallowed_tools=disallowed_tools,  # type: ignore[arg-type]
                cli_path=getattr(base_options, "cli_path", None),
                include_partial_messages=False,
                sandbox=getattr(base_options, "sandbox", None),
                system_prompt=getattr(base_options, "system_prompt", None),
                setting_sources=getattr(base_options, "setting_sources", None),
                stderr=getattr(base_options, "stderr", None),
            )
            recovery_options.resume = session_id
            recovery_options.mcp_servers = getattr(base_options, "mcp_servers", {})
            can_use_tool = getattr(base_options, "can_use_tool", None)
            if can_use_tool is not None:
                recovery_options.can_use_tool = can_use_tool
            return recovery_options

        attempts: List[ClaudeAgentOptions] = [
            _make_recovery_options(allowed_tools=[], disallowed_tools=None)
        ]

        # Fallback for SDK/CLI versions that do not accept an empty allow-list:
        # disallow every configured tool instead.
        configured_tools = list(self.config.claude_allowed_tools or [])
        if configured_tools:
            attempts.append(
                _make_recovery_options(
                    allowed_tools=None,
                    disallowed_tools=configured_tools,
                )
            )

        last_error: Optional[BaseException] = None
        for recovery_options in attempts:
            recovery_messages: List[Message] = []
            try:
                await self._collect_client_messages(
                    options=recovery_options,
                    prompt=TURN_LIMIT_RECOVERY_PROMPT,
                    messages=recovery_messages,
                    stream_callback=None,
                    session_id=session_id,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Turn-limit recovery query failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    used_empty_allow_list=getattr(
                        recovery_options, "allowed_tools", None
                    )
                    == [],
                )
                continue

            cost = 0.0
            num_turns = 0
            recovery_session_id: Optional[str] = None
            result_content: Optional[str] = None
            for message in recovery_messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    recovery_session_id = getattr(message, "session_id", None)
                    result_content = getattr(message, "result", None)
                    num_turns = getattr(message, "num_turns", 0) or 0
                    break

            if result_content is not None:
                content = str(result_content).strip()
            else:
                parts = [
                    self._extract_assistant_text(msg)
                    for msg in recovery_messages
                    if isinstance(msg, AssistantMessage)
                ]
                content = "\n".join(part for part in parts if part).strip()

            if content:
                return content, cost, num_turns, recovery_session_id

        if last_error is not None:
            raise last_error
        return "", 0.0, 0, None

    def _append_turn_limit_note(self, content: str) -> str:
        note = TURN_LIMIT_NOTE.format(turn_limit=self.config.claude_max_turns)
        if note.strip() in content:
            return content
        return (content or "").rstrip() + note

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        user_id: Optional[int] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK."""
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            # Capture stderr from Claude CLI for better error diagnostics
            stderr_lines: List[str] = []

            def _stderr_callback(line: str) -> None:
                stderr_lines.append(line)
                logger.debug("Claude CLI stderr", line=line)

            # Build system prompt, loading CLAUDE.md from working directory if present
            base_prompt = (
                f"All file operations must stay within {working_directory}. "
                "Use relative paths."
            )
            claude_md_path = Path(working_directory) / "CLAUDE.md"
            if claude_md_path.exists():
                base_prompt += "\n\n" + claude_md_path.read_text(encoding="utf-8")
                logger.info(
                    "Loaded CLAUDE.md into system prompt",
                    path=str(claude_md_path),
                )

            # Load per-user memory files (SOUL, USER, MEMORY)
            memory_prompt = self._build_memory_prompt(user_id)
            if memory_prompt:
                base_prompt += "\n\n" + memory_prompt
                logger.info(
                    "Loaded memory files into system prompt",
                    user_id=user_id,
                    memory_length=len(memory_prompt),
                )

            # Build the per-user in-process memory MCP server. Each invocation
            # gets a fresh closure capturing this user's memory directory, so
            # concurrent users stay isolated. Skipped if no memory_dir or no
            # user_id (e.g. anonymous webhook flows).
            memory_server = None
            memory_base_dir: Optional[Path] = None
            resolved_memory_dir = self.config.resolved_memory_dir
            if resolved_memory_dir and user_id is not None:
                memory_base_dir = (
                    resolved_memory_dir / "users" / str(user_id) / "memory"
                )
                memory_server = make_memory_server(str(user_id), memory_base_dir)
                base_prompt += "\n\n" + MEMORY_PROTOCOL_PROMPT
                logger.info(
                    "Memory tool enabled",
                    user_id=user_id,
                    memory_base_dir=str(memory_base_dir),
                )

            # When DISABLE_TOOL_VALIDATION=true, pass None for allowed/disallowed
            # tools so the SDK does not restrict tool usage (e.g. MCP tools).
            if self.config.disable_tool_validation:
                sdk_allowed_tools = None
                sdk_disallowed_tools = None
            else:
                sdk_allowed_tools = list(self.config.claude_allowed_tools or [])
                sdk_disallowed_tools = self.config.claude_disallowed_tools
                if memory_server is not None:
                    # Add the memory tool names so the SDK doesn't deny them.
                    # Tool names follow mcp__{server_name}__{tool_name} format.
                    for cmd in (
                        "view",
                        "create",
                        "str_replace",
                        "insert",
                        "delete",
                        "rename",
                    ):
                        name = f"mcp__memory__{cmd}"
                        if name not in sdk_allowed_tools:
                            sdk_allowed_tools.append(name)

            # Build Claude Agent options
            options = ClaudeAgentOptions(
                max_turns=self.config.claude_max_turns,
                model=self.config.claude_model or None,
                max_budget_usd=self.config.claude_max_cost_per_request,
                cwd=str(working_directory),
                allowed_tools=sdk_allowed_tools,  # type: ignore[arg-type]
                disallowed_tools=sdk_disallowed_tools,  # type: ignore[arg-type]
                cli_path=self.config.claude_cli_path or None,
                include_partial_messages=stream_callback is not None,
                sandbox={
                    "enabled": self.config.sandbox_enabled,
                    "autoAllowBashIfSandboxed": True,
                    "excludedCommands": self.config.sandbox_excluded_commands or [],
                },
                system_prompt=base_prompt,
                setting_sources=["user", "project"],
                stderr=_stderr_callback,
            )

            # Pass MCP server configuration if enabled (file-based config)
            mcp_servers: Dict[str, Any] = {}
            if self.config.enable_mcp and self.config.mcp_config_path:
                mcp_servers.update(self._load_mcp_config(self.config.mcp_config_path))
                logger.info(
                    "MCP servers configured",
                    mcp_config_path=str(self.config.mcp_config_path),
                )

            # Merge in-process memory server (per-user, captures user's base_dir
            # via closure). Always set on options if any servers exist.
            if memory_server is not None:
                mcp_servers["memory"] = memory_server

            if mcp_servers:
                options.mcp_servers = mcp_servers

            # Wire can_use_tool callback for preventive tool validation
            if self.security_validator:
                options.can_use_tool = _make_can_use_tool_callback(
                    security_validator=self.security_validator,
                    working_directory=working_directory,
                    approved_directory=self.config.approved_directory,
                )

            # Resume previous session if we have a session_id
            if session_id and continue_session:
                options.resume = session_id
                logger.info(
                    "Resuming previous session",
                    session_id=session_id,
                )

            # Publish session start to dashboard
            await self._publish_dashboard_event(
                event_kind="SESSION_START",
                content=f"Session started in {working_directory}",
                session_id=session_id,
                user_id=user_id,
            )

            # Collect messages via ClaudeSDKClient
            messages: List[Message] = []
            interrupted = False

            # Execute with timeout and retry, racing against optional interrupt
            max_attempts = max(1, self.config.claude_retry_max_attempts)
            last_exc: Optional[BaseException] = None

            for attempt in range(max_attempts):
                # Reset message accumulator each attempt so that a failed attempt
                # does not pollute the next one with partial/duplicate messages.
                # The collector appends into `messages`, so each retry starts
                # from a clean accumulator.
                messages.clear()

                if attempt > 0:
                    delay = min(
                        self.config.claude_retry_base_delay
                        * (self.config.claude_retry_backoff_factor ** (attempt - 1)),
                        self.config.claude_retry_max_delay,
                    )
                    logger.warning(
                        "Retrying Claude SDK command",
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)

                run_task = asyncio.create_task(
                    self._collect_client_messages(
                        options=options,
                        prompt=prompt,
                        messages=messages,
                        stream_callback=stream_callback,
                        session_id=session_id,
                        user_id=user_id,
                        images=images,
                    )
                )

                interrupt_watcher: Optional["asyncio.Task[None]"] = None
                if interrupt_event is not None:

                    async def _cancel_on_interrupt() -> None:
                        nonlocal interrupted
                        await interrupt_event.wait()
                        interrupted = True
                        run_task.cancel()

                    interrupt_watcher = asyncio.create_task(_cancel_on_interrupt())

                # Note: asyncio.TimeoutError is intentionally NOT retried —
                # it reflects a user-configured hard limit.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(run_task),
                        timeout=self.config.claude_timeout_seconds,
                    )
                    break  # success — exit retry loop
                except asyncio.CancelledError:
                    if not interrupted:
                        raise
                    # Interrupt cancelled the task — wait for cleanup
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    break  # user interrupted — don't retry
                except asyncio.TimeoutError:
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    raise  # timeout — don't retry
                except CLIConnectionError as exc:
                    if self._is_retryable_error(exc) and attempt < max_attempts - 1:
                        last_exc = exc
                        logger.warning(
                            "Transient connection error, will retry",
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        continue
                    raise  # non-retryable or attempts exhausted
                finally:
                    if interrupt_watcher is not None:
                        interrupt_watcher.cancel()
            else:
                if last_exc is not None:
                    raise last_exc

            # Extract cost, tools, and session_id from result message
            cost = 0.0
            tools_used: List[Dict[str, Any]] = []
            claude_session_id = None
            result_content = None
            result_num_turns: Optional[int] = None
            for message in messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    claude_session_id = getattr(message, "session_id", None)
                    result_content = getattr(message, "result", None)
                    result_num_turns = getattr(message, "num_turns", None)
                    current_time = asyncio.get_event_loop().time()
                    for msg in messages:
                        if isinstance(msg, AssistantMessage):
                            msg_content = getattr(msg, "content", [])
                            if msg_content and isinstance(msg_content, list):
                                for block in msg_content:
                                    if isinstance(block, ToolUseBlock):
                                        tools_used.append(
                                            {
                                                "name": getattr(
                                                    block, "name", "unknown"
                                                ),
                                                "timestamp": current_time,
                                                "input": getattr(block, "input", {}),
                                            }
                                        )
                    break

            # Fallback: extract session_id from StreamEvent messages if
            # ResultMessage didn't provide one (can happen with some CLI versions)
            if not claude_session_id:
                for message in messages:
                    msg_session_id = getattr(message, "session_id", None)
                    if msg_session_id and not isinstance(message, ResultMessage):
                        claude_session_id = msg_session_id
                        logger.info(
                            "Got session ID from stream event (fallback)",
                            session_id=claude_session_id,
                        )
                        break

            # Use Claude's session_id if available, otherwise fall back
            final_session_id = claude_session_id or session_id or ""

            if claude_session_id and claude_session_id != session_id:
                logger.info(
                    "Got session ID from Claude",
                    claude_session_id=claude_session_id,
                    previous_session_id=session_id,
                )

            # Use ResultMessage.result if available, fall back to message extraction
            if result_content is not None:
                content = str(result_content).strip()
            else:
                content_parts = []
                for msg in messages:
                    if isinstance(msg, AssistantMessage):
                        msg_content = getattr(msg, "content", [])
                        if msg_content and isinstance(msg_content, list):
                            for block in msg_content:
                                if hasattr(block, "text"):
                                    content_parts.append(block.text)
                        elif msg_content:
                            content_parts.append(str(msg_content))
                content = "\n".join(content_parts).strip()

            if not content and tools_used:
                tool_names = [
                    tool.get("name", "")
                    for tool in tools_used
                    if isinstance(tool.get("name"), str) and tool.get("name")
                ]
                unique_tool_names = list(dict.fromkeys(tool_names))
                tools_summary = ", ".join(unique_tool_names) or "unknown"
                content = TASK_COMPLETED_MSG.format(tools_summary=tools_summary)

            hit_turn_limit = self._should_attempt_turn_limit_recovery(
                result_num_turns=result_num_turns,
                content=content,
                messages=messages,
                interrupted=interrupted,
            )
            recovery_attempted = False
            recovery_succeeded = False

            if hit_turn_limit:
                logger.warning(
                    "Claude likely hit max_turns",
                    session_id=final_session_id,
                    turn_limit=self.config.claude_max_turns,
                    result_num_turns=result_num_turns,
                    has_session_id=bool(final_session_id),
                )
                if final_session_id:
                    recovery_attempted = True
                    try:
                        recovery_content, recovery_cost, recovery_turns, recovery_id = (
                            await self._attempt_turn_limit_recovery(
                                base_options=options,
                                session_id=final_session_id,
                            )
                        )
                        cost += recovery_cost
                        if recovery_turns:
                            result_num_turns = (result_num_turns or 0) + recovery_turns
                        if recovery_id and recovery_id != final_session_id:
                            logger.info(
                                "Turn-limit recovery returned session ID",
                                recovery_session_id=recovery_id,
                                original_session_id=final_session_id,
                            )
                        if recovery_content:
                            content = recovery_content
                            recovery_succeeded = True
                    except Exception as recovery_error:
                        logger.warning(
                            "Turn-limit recovery failed",
                            session_id=final_session_id,
                            error=str(recovery_error),
                            error_type=type(recovery_error).__name__,
                        )
                else:
                    logger.warning(
                        "Skipping turn-limit recovery because session_id is missing"
                    )

                content = self._append_turn_limit_note(content)

            # Calculate duration after any recovery attempt so telemetry includes
            # the full user-visible request.
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Publish final response to dashboard
            await self._publish_dashboard_event(
                event_kind="RESPONSE",
                content=content[:500] if content else "",
                session_id=final_session_id,
                user_id=user_id,
            )

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=result_num_turns
                if result_num_turns is not None
                else len(
                    [
                        m
                        for m in messages
                        if isinstance(m, (UserMessage, AssistantMessage))
                    ]
                ),
                tools_used=tools_used,
                interrupted=interrupted,
                hit_turn_limit=hit_turn_limit,
                turn_limit=self.config.claude_max_turns if hit_turn_limit else None,
                turn_limit_recovery_attempted=recovery_attempted,
                turn_limit_recovery_succeeded=recovery_succeeded,
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK command timed out",
                timeout_seconds=self.config.claude_timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Claude SDK timed out after {self.config.claude_timeout_seconds}s"
            )

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code\n\n"
                "If already installed, try one of these:\n"
                "  1. Add Claude to your PATH\n"
                "  2. Create a symlink: ln -s $(which claude) /usr/local/bin/claude\n"
                "  3. Set CLAUDE_CLI_PATH environment variable"
            )
            raise ClaudeProcessError(error_msg)

        except ProcessError as e:
            error_str = str(e)
            # Include captured stderr for better diagnostics
            captured_stderr = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
            if captured_stderr:
                error_str = f"{error_str}\nStderr: {captured_stderr}"
            logger.error(
                "Claude process failed",
                error=error_str,
                exit_code=getattr(e, "exit_code", None),
                stderr=captured_stderr or None,
            )
            # Check if the process error is MCP-related
            if "mcp" in error_str.lower():
                raise ClaudeMCPError(f"MCP server error: {error_str}")
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            error_str = str(e)
            logger.error("Claude connection error", error=error_str)
            # Check if the connection error is MCP-related
            if "mcp" in error_str.lower() or "server" in error_str.lower():
                raise ClaudeMCPError(f"MCP server connection failed: {error_str}")
            raise ClaudeProcessError(f"Failed to connect to Claude: {error_str}")

        except CLIJSONDecodeError as e:
            logger.error("Claude SDK JSON decode error", error=str(e))
            raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

        except ClaudeSDKError as e:
            logger.error("Claude SDK error", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

        except Exception as e:
            exceptions = getattr(e, "exceptions", None)
            if exceptions is not None:
                # ExceptionGroup from TaskGroup operations (Python 3.11+)
                logger.error(
                    "Task group error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                    exception_count=len(exceptions),
                    exceptions=[str(ex) for ex in exceptions[:3]],
                )
                raise ClaudeProcessError(
                    f"Claude SDK task error: {exceptions[0] if exceptions else e}"
                )

            logger.error(
                "Unexpected error in Claude SDK",
                error=str(e),
                error_type=type(e).__name__,
            )
            raise ClaudeProcessError(f"Unexpected error: {str(e)}")

    async def _handle_stream_message(
        self,
        message: Message,
        stream_callback: Callable[[StreamUpdate], None],
        session_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> None:
        """Handle streaming message from claude-agent-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                content = getattr(message, "content", [])
                text_parts = []
                tool_calls = []

                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(
                                {
                                    "name": block.name,
                                    "input": block.input,
                                    "id": block.id,
                                }
                            )
                        elif isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ThinkingBlock):
                            text_parts.append(block.thinking)

                if text_parts or tool_calls:
                    update = StreamUpdate(
                        type="assistant",
                        content=("\n".join(text_parts) if text_parts else None),
                        tool_calls=tool_calls if tool_calls else None,
                    )
                    await stream_callback(update)

                    # Publish dashboard events
                    if text_parts:
                        await self._publish_dashboard_event(
                            event_kind="THINKING",
                            content="\n".join(text_parts),
                            session_id=session_id,
                            user_id=user_id,
                        )
                    for tc in tool_calls:
                        await self._publish_dashboard_event(
                            event_kind="TOOL_CALL",
                            content=tc.get("name", "unknown"),
                            tool_name=tc.get("name"),
                            tool_input=tc.get("input"),
                            session_id=session_id,
                            user_id=user_id,
                        )
                elif content:
                    # Fallback for non-list content
                    update = StreamUpdate(
                        type="assistant",
                        content=str(content),
                    )
                    await stream_callback(update)

            elif isinstance(message, StreamEvent):
                event = message.event or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            update = StreamUpdate(
                                type="stream_delta",
                                content=text,
                            )
                            await stream_callback(update)

            elif isinstance(message, UserMessage):
                content = getattr(message, "content", "")
                if content:
                    update = StreamUpdate(
                        type="user",
                        content=content,
                    )
                    await stream_callback(update)
                    await self._publish_dashboard_event(
                        event_kind="TOOL_RESULT",
                        content=str(content)[:500],
                        session_id=session_id,
                        user_id=user_id,
                    )

        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    async def _publish_dashboard_event(
        self,
        event_kind: str,
        content: str,
        session_id: Optional[str] = None,
        user_id: Optional[int] = None,
        tool_name: Optional[str] = None,
        tool_input: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a DashboardStreamEvent to the EventBus if available."""
        if not self.event_bus:
            return
        try:
            event = DashboardStreamEvent(
                event_kind=event_kind,
                session_id=session_id or "",
                user_id=user_id or 0,
                content=content,
                tool_name=tool_name,
                tool_input=tool_input,
            )
            await self.event_bus.publish(event)
        except Exception:
            pass  # never let dashboard publishing break the main flow

    def _load_mcp_config(self, config_path: Path) -> Dict[str, Any]:
        """Load MCP server configuration from a JSON file.

        The new claude-agent-sdk expects mcp_servers as a dict, not a file path.
        """
        import json

        try:
            with open(config_path) as f:
                config_data = json.load(f)
            return config_data.get("mcpServers", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load MCP config", path=str(config_path), error=str(e)
            )
            return {}
