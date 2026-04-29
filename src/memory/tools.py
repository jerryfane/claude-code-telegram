"""In-process MCP memory tools mirroring Anthropic's official memory_20250818 tool.

Exposes six commands as @tool-decorated async functions:
    view, create, str_replace, insert, delete, rename

Each per-Telegram-user invocation builds its own server via ``make_memory_server``,
which captures the user's base directory in closures so concurrent users stay
isolated. Output formatting matches Anthropic's published spec verbatim so the
model responds to the results the same way it would to the official tool.

The ``_op_*`` functions hold the actual logic and take ``base_dir`` explicitly
so they're unit-testable without spinning up an MCP server.

Reference: https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from claude_agent_sdk import create_sdk_mcp_server, tool

from .path_security import MemoryPathError, resolve_memory_path

logger = structlog.get_logger()


# ----- Output helpers (match Anthropic's spec verbatim) -----


def _err(text: str) -> Dict[str, Any]:
    """Build an is_error tool result without raising — keeps the agent loop alive."""
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _ok(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    return f"{n / (1024 * 1024 * 1024):.1f}G"


def _format_directory_listing(directory: Path, claude_path: str) -> str:
    """Format directory contents up to 2 levels deep, excluding hidden + node_modules."""
    lines = [
        f"Here're the files and directories up to 2 levels deep in {claude_path}, "
        "excluding hidden items and node_modules:"
    ]

    def _walk(d: Path, depth: int, claude_prefix: str) -> List[str]:
        out: List[str] = []
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return out
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name == "node_modules":
                continue
            display = f"{claude_prefix}/{name}" if claude_prefix else name
            try:
                if entry.is_dir():
                    out.append(f"4.0K\t{display}")
                    if depth < 2:
                        out.extend(_walk(entry, depth + 1, display))
                else:
                    size = entry.stat().st_size
                    out.append(f"{_human_size(size)}\t{display}")
            except OSError:
                continue
        return out

    lines.append(f"4.0K\t{claude_path}")
    lines.extend(_walk(directory, 1, claude_path))
    return "\n".join(lines)


# ----- Pure operation functions (testable without an MCP server) -----


def _op_view(
    base_dir: Path, claude_path: str, view_range: Optional[List[int]] = None
) -> Dict[str, Any]:
    try:
        target = resolve_memory_path(base_dir, claude_path)
    except MemoryPathError as e:
        return _err(f"Error: {e}")
    try:
        if not target.exists():
            return _err(
                f"The path {claude_path} does not exist. "
                "Please provide a valid path."
            )
        if target.is_dir():
            return _ok(_format_directory_listing(target, claude_path))
        text = target.read_text(encoding="utf-8")
        all_lines = text.splitlines()
        total = len(all_lines)
        if total > 999_999:
            return _err(
                f"File {claude_path} exceeds maximum line limit of 999,999 lines."
            )
        start, end = 1, total
        if view_range and isinstance(view_range, list) and len(view_range) == 2:
            start = max(1, int(view_range[0]))
            end_raw = int(view_range[1])
            end = total if end_raw == -1 else min(total, end_raw)
        header = f"Here's the content of {claude_path} with line numbers:"
        body = "\n".join(f"{i:>6}\t{all_lines[i - 1]}" for i in range(start, end + 1))
        return _ok(f"{header}\n{body}" if body else header)
    except OSError as e:
        return _err(f"Error reading {claude_path}: {e}")


def _op_create(base_dir: Path, claude_path: str, file_text: str) -> Dict[str, Any]:
    try:
        target = resolve_memory_path(base_dir, claude_path)
    except MemoryPathError as e:
        return _err(f"Error: {e}")
    if target.exists():
        return _err(f"Error: File {claude_path} already exists")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_text, encoding="utf-8")
        return _ok(f"File created successfully at: {claude_path}")
    except OSError as e:
        return _err(f"Error creating {claude_path}: {e}")


def _op_str_replace(
    base_dir: Path, claude_path: str, old_str: str, new_str: str
) -> Dict[str, Any]:
    try:
        target = resolve_memory_path(base_dir, claude_path)
    except MemoryPathError as e:
        return _err(f"Error: {e}")
    if not target.exists() or target.is_dir():
        return _err(
            f"Error: The path {claude_path} does not exist. "
            "Please provide a valid path."
        )
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as e:
        return _err(f"Error reading {claude_path}: {e}")

    count = text.count(old_str)
    if count == 0:
        return _err(
            f"No replacement was performed, old_str `{old_str}` "
            f"did not appear verbatim in {claude_path}."
        )
    if count > 1:
        line_nums: List[int] = []
        for idx, line in enumerate(text.splitlines(), start=1):
            if old_str in line:
                line_nums.append(idx)
        return _err(
            f"No replacement was performed. Multiple occurrences of "
            f"old_str `{old_str}` in lines: "
            f"{line_nums if line_nums else 'multiple'}. "
            "Please ensure it is unique"
        )

    new_text = text.replace(old_str, new_str, 1)
    try:
        target.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return _err(f"Error writing {claude_path}: {e}")

    new_lines = new_text.splitlines()
    snippet_start = 1
    if new_str:
        idx = new_text.find(new_str)
        if idx >= 0:
            snippet_start = max(1, new_text[:idx].count("\n") - 2 + 1)
    snippet_end = min(len(new_lines), snippet_start + 6)
    snippet = "\n".join(
        f"{i:>6}\t{new_lines[i - 1]}" for i in range(snippet_start, snippet_end + 1)
    )
    return _ok(f"The memory file has been edited.\n{snippet}")


def _op_insert(
    base_dir: Path, claude_path: str, insert_line: int, insert_text: str
) -> Dict[str, Any]:
    try:
        target = resolve_memory_path(base_dir, claude_path)
    except MemoryPathError as e:
        return _err(f"Error: {e}")
    if not target.exists() or target.is_dir():
        return _err(f"Error: The path {claude_path} does not exist")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as e:
        return _err(f"Error reading {claude_path}: {e}")

    lines = text.splitlines(keepends=True)
    n_lines = len(lines)
    if insert_line < 0 or insert_line > n_lines:
        return _err(
            f"Error: Invalid `insert_line` parameter: {insert_line}. "
            f"It should be within the range of lines of the file: [0, {n_lines}]"
        )

    chunk = insert_text if insert_text.endswith("\n") else insert_text + "\n"
    new_lines = lines[:insert_line] + [chunk] + lines[insert_line:]
    try:
        target.write_text("".join(new_lines), encoding="utf-8")
    except OSError as e:
        return _err(f"Error writing {claude_path}: {e}")
    return _ok(f"The file {claude_path} has been edited.")


def _op_delete(base_dir: Path, claude_path: str) -> Dict[str, Any]:
    try:
        target = resolve_memory_path(base_dir, claude_path)
    except MemoryPathError as e:
        return _err(f"Error: {e}")
    if not target.exists():
        return _err(f"Error: The path {claude_path} does not exist")
    if target.resolve() == base_dir.resolve():
        return _err("Error: cannot delete the root memory directory")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as e:
        return _err(f"Error deleting {claude_path}: {e}")
    return _ok(f"Successfully deleted {claude_path}")


def _op_rename(base_dir: Path, old_claude: str, new_claude: str) -> Dict[str, Any]:
    try:
        src = resolve_memory_path(base_dir, old_claude)
        dst = resolve_memory_path(base_dir, new_claude)
    except MemoryPathError as e:
        return _err(f"Error: {e}")
    if not src.exists():
        return _err(f"Error: The path {old_claude} does not exist")
    if dst.exists():
        return _err(f"Error: The destination {new_claude} already exists")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    except OSError as e:
        return _err(f"Error renaming {old_claude} to {new_claude}: {e}")
    return _ok(f"Successfully renamed {old_claude} to {new_claude}")


# ----- Factory: builds the @tool-decorated MCP server bound to a base_dir -----


def make_memory_server(user_id: str, base_dir: Path) -> Any:
    """Build an in-process MCP server scoped to ``base_dir``.

    Args:
        user_id: Telegram user id (used for log enrichment only — scoping is
            done via the closure-captured ``base_dir``).
        base_dir: Absolute path under which all /memories paths resolve.
            Will be created if it does not exist.

    Returns:
        McpSdkServerConfig — pass to ClaudeAgentOptions.mcp_servers as
        ``{"memory": <server>}``. Tools become available as
        ``mcp__memory__<command>``.
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    @tool(
        "view",
        "View a file or directory in the memory store. For files, returns "
        "line-numbered contents (optionally restricted to a view_range). "
        "For directories, returns a listing up to 2 levels deep.",
        {"path": str},
    )
    async def view(args: Dict[str, Any]) -> Dict[str, Any]:
        result = _op_view(base_dir, args["path"], args.get("view_range"))
        if not result.get("is_error"):
            logger.debug("memory.view", user_id=user_id, path=args["path"])
        return result

    @tool(
        "create",
        "Create a new file in the memory store. Errors if the file already exists.",
        {"path": str, "file_text": str},
    )
    async def create(args: Dict[str, Any]) -> Dict[str, Any]:
        result = _op_create(base_dir, args["path"], args["file_text"])
        if not result.get("is_error"):
            logger.info("memory.create", user_id=user_id, path=args["path"])
        return result

    @tool(
        "str_replace",
        "Replace a unique substring in a memory file.",
        {"path": str, "old_str": str, "new_str": str},
    )
    async def str_replace(args: Dict[str, Any]) -> Dict[str, Any]:
        result = _op_str_replace(
            base_dir, args["path"], args["old_str"], args["new_str"]
        )
        if not result.get("is_error"):
            logger.info("memory.str_replace", user_id=user_id, path=args["path"])
        return result

    @tool(
        "insert",
        "Insert text at a specific line in a memory file. "
        "Use insert_line=0 to prepend.",
        {"path": str, "insert_line": int, "insert_text": str},
    )
    async def insert(args: Dict[str, Any]) -> Dict[str, Any]:
        result = _op_insert(
            base_dir, args["path"], int(args["insert_line"]), args["insert_text"]
        )
        if not result.get("is_error"):
            logger.info("memory.insert", user_id=user_id, path=args["path"])
        return result

    @tool(
        "delete",
        "Delete a file or directory (recursive) from the memory store.",
        {"path": str},
    )
    async def delete(args: Dict[str, Any]) -> Dict[str, Any]:
        result = _op_delete(base_dir, args["path"])
        if not result.get("is_error"):
            logger.info("memory.delete", user_id=user_id, path=args["path"])
        return result

    @tool(
        "rename",
        "Rename or move a file or directory within the memory store.",
        {"old_path": str, "new_path": str},
    )
    async def rename(args: Dict[str, Any]) -> Dict[str, Any]:
        result = _op_rename(base_dir, args["old_path"], args["new_path"])
        if not result.get("is_error"):
            logger.info(
                "memory.rename",
                user_id=user_id,
                old_path=args["old_path"],
                new_path=args["new_path"],
            )
        return result

    return create_sdk_mcp_server(
        name="memory",
        version="1.0.0",
        tools=[view, create, str_replace, insert, delete, rename],
    )
