"""Path validation tests for the memory tool.

These are defense-in-depth tests on top of SecurityValidator. The memory tool
must never let Claude read or write outside the per-user memory directory,
even via traversal sequences, URL-encoded paths, or symlinks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.memory.path_security import MEMORY_PREFIX, MemoryPathError, resolve_memory_path


def test_basic_root_path(tmp_path: Path) -> None:
    """`/memories` itself resolves to the base directory."""
    result = resolve_memory_path(tmp_path, "/memories")
    assert result == tmp_path.resolve()


def test_basic_file_path(tmp_path: Path) -> None:
    """`/memories/foo.md` maps to base_dir/foo.md."""
    result = resolve_memory_path(tmp_path, "/memories/foo.md")
    assert result == (tmp_path / "foo.md").resolve()


def test_nested_path(tmp_path: Path) -> None:
    """Subdirectories work."""
    result = resolve_memory_path(tmp_path, "/memories/people/alice.md")
    assert result == (tmp_path / "people" / "alice.md").resolve()


def test_rejects_missing_prefix(tmp_path: Path) -> None:
    """Paths that don't start with `/memories` are rejected."""
    with pytest.raises(MemoryPathError, match="must start with"):
        resolve_memory_path(tmp_path, "/etc/passwd")


def test_rejects_relative_path(tmp_path: Path) -> None:
    """Relative paths (no leading slash) are rejected."""
    with pytest.raises(MemoryPathError, match="must start with"):
        resolve_memory_path(tmp_path, "memories/foo.md")


def test_rejects_empty_string(tmp_path: Path) -> None:
    with pytest.raises(MemoryPathError, match="non-empty"):
        resolve_memory_path(tmp_path, "")


def test_rejects_non_string(tmp_path: Path) -> None:
    with pytest.raises(MemoryPathError, match="non-empty"):
        resolve_memory_path(tmp_path, None)  # type: ignore[arg-type]


def test_rejects_traversal_dotdot(tmp_path: Path) -> None:
    """`..` segments that escape base_dir are rejected."""
    with pytest.raises(MemoryPathError, match="escapes"):
        resolve_memory_path(tmp_path, "/memories/../../../etc/passwd")


def test_rejects_traversal_url_encoded(tmp_path: Path) -> None:
    """URL-encoded `..` is decoded before validation."""
    with pytest.raises(MemoryPathError, match="escapes"):
        resolve_memory_path(tmp_path, "/memories/%2e%2e/%2e%2e/etc/passwd")


def test_rejects_null_byte(tmp_path: Path) -> None:
    with pytest.raises(MemoryPathError, match="NUL"):
        resolve_memory_path(tmp_path, "/memories/foo\x00.md")


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    """Symlinks pointing outside base_dir are rejected when resolved."""
    outside = tmp_path.parent / "outside_memory_dir"
    outside.mkdir()
    base = tmp_path / "memdir"
    base.mkdir()
    # Create symlink inside base pointing outside
    (base / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemoryPathError, match="escapes"):
        resolve_memory_path(base, "/memories/escape")


def test_internal_dotdot_within_base_is_ok(tmp_path: Path) -> None:
    """`..` that stays within base_dir is fine after resolution."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # /memories/a/../b -> tmp_path/b
    result = resolve_memory_path(tmp_path, "/memories/a/../b")
    assert result == (tmp_path / "b").resolve()


def test_constants() -> None:
    assert MEMORY_PREFIX == "/memories"


def test_path_with_trailing_slash(tmp_path: Path) -> None:
    """Trailing slash on directory paths works."""
    (tmp_path / "people").mkdir()
    result = resolve_memory_path(tmp_path, "/memories/people/")
    assert result == (tmp_path / "people").resolve()


def test_rejects_just_prefix_plus_garbage(tmp_path: Path) -> None:
    """`/memoriesX` (no separator after prefix) is rejected."""
    with pytest.raises(MemoryPathError, match="must start with"):
        resolve_memory_path(tmp_path, "/memoriesX/foo")
