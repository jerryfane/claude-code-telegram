"""Tests for memory tool operations.

These tests target the pure ``_op_*`` helpers — no MCP server, no claude-agent-sdk
dependency at runtime. The closures inside ``make_memory_server`` are thin
delegates to these.
"""

from __future__ import annotations

from pathlib import Path

from src.memory.tools import (
    _op_create,
    _op_delete,
    _op_insert,
    _op_rename,
    _op_str_replace,
    _op_view,
)

# ----- view -----


def test_view_directory(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hi")
    result = _op_view(tmp_path, "/memories")
    assert "is_error" not in result
    text = result["content"][0]["text"]
    assert "/memories/a.md" in text
    assert "Here're the files and directories" in text


def test_view_directory_excludes_hidden(tmp_path: Path) -> None:
    (tmp_path / ".hidden").write_text("secret")
    (tmp_path / "visible.md").write_text("ok")
    result = _op_view(tmp_path, "/memories")
    text = result["content"][0]["text"]
    assert "visible.md" in text
    assert ".hidden" not in text


def test_view_file_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("alpha\nbeta\ngamma")
    result = _op_view(tmp_path, "/memories/f.md")
    text = result["content"][0]["text"]
    assert "Here's the content of /memories/f.md with line numbers:" in text
    # Right-aligned 6-char + tab
    assert "     1\talpha" in text
    assert "     2\tbeta" in text
    assert "     3\tgamma" in text


def test_view_file_with_range(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("a\nb\nc\nd\ne")
    result = _op_view(tmp_path, "/memories/f.md", view_range=[2, 3])
    text = result["content"][0]["text"]
    assert "     2\tb" in text
    assert "     3\tc" in text
    assert "     1\ta" not in text
    assert "     4\td" not in text


def test_view_missing_path(tmp_path: Path) -> None:
    result = _op_view(tmp_path, "/memories/nope.md")
    assert result.get("is_error") is True
    assert "does not exist" in result["content"][0]["text"]


def test_view_traversal_rejected(tmp_path: Path) -> None:
    result = _op_view(tmp_path, "/memories/../../etc/passwd")
    assert result.get("is_error") is True
    assert "escapes" in result["content"][0]["text"]


# ----- create -----


def test_create_new_file(tmp_path: Path) -> None:
    result = _op_create(tmp_path, "/memories/new.md", "hello world")
    assert "is_error" not in result
    assert (tmp_path / "new.md").read_text() == "hello world"
    assert "File created successfully" in result["content"][0]["text"]


def test_create_in_subdirectory_makes_parents(tmp_path: Path) -> None:
    result = _op_create(tmp_path, "/memories/people/alice.md", "she likes terse")
    assert "is_error" not in result
    assert (tmp_path / "people" / "alice.md").read_text() == "she likes terse"


def test_create_existing_file_errors(tmp_path: Path) -> None:
    (tmp_path / "exists.md").write_text("already here")
    result = _op_create(tmp_path, "/memories/exists.md", "overwritten")
    assert result.get("is_error") is True
    assert "already exists" in result["content"][0]["text"]
    # Original content untouched
    assert (tmp_path / "exists.md").read_text() == "already here"


def test_create_traversal_rejected(tmp_path: Path) -> None:
    result = _op_create(tmp_path, "/etc/passwd", "rooted")
    assert result.get("is_error") is True


# ----- str_replace -----


def test_str_replace_unique_match(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("foo\nbar\nbaz\n")
    result = _op_str_replace(tmp_path, "/memories/f.md", "bar", "BAR")
    assert "is_error" not in result
    assert (tmp_path / "f.md").read_text() == "foo\nBAR\nbaz\n"
    assert "The memory file has been edited" in result["content"][0]["text"]


def test_str_replace_no_match(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("foo\nbar\n")
    result = _op_str_replace(tmp_path, "/memories/f.md", "missing", "x")
    assert result.get("is_error") is True
    assert "did not appear verbatim" in result["content"][0]["text"]


def test_str_replace_duplicate_match(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("foo\nfoo\nbar\n")
    result = _op_str_replace(tmp_path, "/memories/f.md", "foo", "FOO")
    assert result.get("is_error") is True
    assert "Multiple occurrences" in result["content"][0]["text"]
    # File untouched
    assert (tmp_path / "f.md").read_text() == "foo\nfoo\nbar\n"


def test_str_replace_missing_file(tmp_path: Path) -> None:
    result = _op_str_replace(tmp_path, "/memories/nope.md", "a", "b")
    assert result.get("is_error") is True


def test_str_replace_on_directory_errors(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    result = _op_str_replace(tmp_path, "/memories/subdir", "a", "b")
    assert result.get("is_error") is True


# ----- insert -----


def test_insert_at_beginning(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("line1\nline2\n")
    result = _op_insert(tmp_path, "/memories/f.md", 0, "header")
    assert "is_error" not in result
    assert (tmp_path / "f.md").read_text() == "header\nline1\nline2\n"


def test_insert_in_middle(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("a\nb\nc\n")
    result = _op_insert(tmp_path, "/memories/f.md", 2, "MIDDLE")
    assert "is_error" not in result
    assert (tmp_path / "f.md").read_text() == "a\nb\nMIDDLE\nc\n"


def test_insert_at_end(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("a\nb\n")
    result = _op_insert(tmp_path, "/memories/f.md", 2, "END")
    assert "is_error" not in result
    assert (tmp_path / "f.md").read_text() == "a\nb\nEND\n"


def test_insert_invalid_line_number(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("a\nb\n")
    result = _op_insert(tmp_path, "/memories/f.md", 99, "nope")
    assert result.get("is_error") is True
    assert "Invalid `insert_line`" in result["content"][0]["text"]


def test_insert_negative_line_number(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("a\nb\n")
    result = _op_insert(tmp_path, "/memories/f.md", -1, "nope")
    assert result.get("is_error") is True


# ----- delete -----


def test_delete_file(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("bye")
    result = _op_delete(tmp_path, "/memories/f.md")
    assert "is_error" not in result
    assert not (tmp_path / "f.md").exists()


def test_delete_directory_recursive(tmp_path: Path) -> None:
    (tmp_path / "dir" / "nested").mkdir(parents=True)
    (tmp_path / "dir" / "nested" / "file.md").write_text("x")
    result = _op_delete(tmp_path, "/memories/dir")
    assert "is_error" not in result
    assert not (tmp_path / "dir").exists()


def test_delete_missing(tmp_path: Path) -> None:
    result = _op_delete(tmp_path, "/memories/nope.md")
    assert result.get("is_error") is True


def test_delete_root_refused(tmp_path: Path) -> None:
    result = _op_delete(tmp_path, "/memories")
    assert result.get("is_error") is True
    assert "root" in result["content"][0]["text"].lower()


# ----- rename -----


def test_rename_file(tmp_path: Path) -> None:
    (tmp_path / "old.md").write_text("content")
    result = _op_rename(tmp_path, "/memories/old.md", "/memories/new.md")
    assert "is_error" not in result
    assert not (tmp_path / "old.md").exists()
    assert (tmp_path / "new.md").read_text() == "content"


def test_rename_into_subdirectory(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("x")
    result = _op_rename(tmp_path, "/memories/f.md", "/memories/people/f.md")
    assert "is_error" not in result
    assert (tmp_path / "people" / "f.md").read_text() == "x"


def test_rename_destination_exists(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")
    result = _op_rename(tmp_path, "/memories/a.md", "/memories/b.md")
    assert result.get("is_error") is True
    assert "already exists" in result["content"][0]["text"]
    assert (tmp_path / "a.md").read_text() == "a"
    assert (tmp_path / "b.md").read_text() == "b"


def test_rename_missing_source(tmp_path: Path) -> None:
    result = _op_rename(tmp_path, "/memories/missing.md", "/memories/dest.md")
    assert result.get("is_error") is True


def test_rename_traversal_rejected(tmp_path: Path) -> None:
    (tmp_path / "src.md").write_text("x")
    result = _op_rename(tmp_path, "/memories/src.md", "/etc/dst")
    assert result.get("is_error") is True
