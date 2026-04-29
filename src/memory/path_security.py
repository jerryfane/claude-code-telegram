"""Path validation for memory tool operations.

Defense-in-depth on top of SecurityValidator. Enforces that all paths Claude
passes to the memory tool are scoped to the per-user memory directory.

Anthropic's official memory tool exposes paths to Claude under ``/memories``.
We honor that convention: Claude sees ``/memories/foo.md`` and we map it to
``<base_dir>/foo.md``. Any path that doesn't start with ``/memories`` or that
resolves outside ``base_dir`` (via ``..``, symlinks, or URL-encoded sequences)
raises MemoryPathError.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

MEMORY_PREFIX = "/memories"


class MemoryPathError(ValueError):
    """Raised when a memory tool path violates scoping rules."""


def resolve_memory_path(base_dir: Path, claude_path: str) -> Path:
    """Map a Claude-side ``/memories/...`` path to a real filesystem path.

    Validates that:
    - ``claude_path`` is a non-empty string
    - It starts with ``/memories`` (or is exactly ``/memories``)
    - After URL-decoding and resolving, the result stays within ``base_dir``

    Returns the resolved Path (which may not exist yet — caller decides).
    Raises MemoryPathError on any violation.
    """
    if not isinstance(claude_path, str) or not claude_path:
        raise MemoryPathError("path must be a non-empty string")

    decoded = unquote(claude_path)

    # Reject embedded NUL bytes outright
    if "\x00" in decoded:
        raise MemoryPathError("path contains NUL byte")

    # Must be the prefix exactly, or have a separator after it
    if decoded != MEMORY_PREFIX and not decoded.startswith(MEMORY_PREFIX + "/"):
        raise MemoryPathError(
            f"path must start with {MEMORY_PREFIX}: got {claude_path!r}"
        )

    # Strip the prefix; treat the remainder as relative to base_dir
    relative = decoded[len(MEMORY_PREFIX) :].lstrip("/")

    # Resolve under base_dir. Use strict=False so non-existent paths are OK
    # (create/rename need to point at paths that don't exist yet).
    base_resolved = base_dir.resolve()
    candidate = (base_resolved / relative).resolve()

    # Ensure candidate is base_dir or a descendant
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise MemoryPathError(
            f"path escapes memory directory: {claude_path!r}"
        ) from exc

    return candidate
