import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.scheduler.code_agent import CodeAgentService, CodeAgentSession


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class FakeStream:
    def __init__(
        self,
        lines: list[bytes] | None = None,
        chunks: list[bytes] | None = None,
    ):
        self._lines = list(lines or [])
        self._chunks = list(chunks or [])

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if self._lines:
            return self._lines.pop(0)
        return b""

    async def read(self, _size: int = -1) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeProcess:
    def __init__(
        self,
        *,
        stdout_lines: list[bytes] | None = None,
        stderr_chunks: list[bytes] | None = None,
        final_returncode: int = 0,
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStream(lines=stdout_lines)
        self.stderr = FakeStream(chunks=stderr_chunks)
        self.returncode: int | None = None
        self.final_returncode = final_returncode
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


async def _start_session(monkeypatch: pytest.MonkeyPatch, process: FakeProcess):
    events: list[dict] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    async def callback(data: dict) -> None:
        events.append(data)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    session = CodeAgentSession(
        task="check the repo",
        working_directory=Path("/tmp"),
        cli_path="/bin/claude",
        output_callback=callback,
        max_duration=60,
    )
    await session.start()
    assert session._read_task is not None
    await session._read_task
    return session, events


@pytest.mark.asyncio
async def test_session_forwards_real_result_and_marks_completed(monkeypatch):
    result = {
        "type": "result",
        "result": "done",
        "total_cost_usd": 0.01,
        "num_turns": 2,
    }
    process = FakeProcess(stdout_lines=[json.dumps(result).encode() + b"\n"])

    session, events = await _start_session(monkeypatch, process)

    assert events == [result]
    assert session.status == "completed"
    assert session.total_cost == 0.01
    assert session.num_turns == 2
    assert process.stdin.writes


@pytest.mark.asyncio
async def test_nonzero_exit_without_result_emits_diagnostic(monkeypatch):
    process = FakeProcess(
        stderr_chunks=[b"twikit failed with Cloudflare 403"], final_returncode=2
    )

    session, events = await _start_session(monkeypatch, process)

    assert session.status == "failed"
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "result"
    assert event["subtype"] == "code_agent_diagnostic"
    assert event["is_error"] is True
    assert "Exit code: 2" in event["result"]
    assert "Cloudflare 403" in event["result"]


@pytest.mark.asyncio
async def test_non_json_stdout_is_included_in_missing_result_diagnostic(monkeypatch):
    process = FakeProcess(stdout_lines=[b"not-json output\n"], final_returncode=1)

    session, events = await _start_session(monkeypatch, process)

    assert session.status == "failed"
    assert events[0]["is_error"] is True
    assert "non-JSON stdout tail" in events[0]["result"]
    assert "not-json output" in events[0]["result"]


@pytest.mark.asyncio
async def test_timeout_emits_diagnostic_and_terminates_process():
    events: list[dict] = []

    async def callback(data: dict) -> None:
        events.append(data)

    process = FakeProcess()
    process.returncode = None
    session = CodeAgentSession(
        task="hang forever",
        working_directory=Path("/tmp"),
        cli_path="/bin/claude",
        output_callback=callback,
        max_duration=0,
    )
    session._process = process
    session.status = "running"

    await session._timeout_watchdog()

    assert session.status == "failed"
    assert process.terminated is True
    assert len(events) == 1
    assert events[0]["is_error"] is True
    assert "timed out" in events[0]["result"]


@pytest.mark.asyncio
async def test_service_passes_default_model_to_spawned_session(monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr(CodeAgentSession, "start", start)

    service = CodeAgentService(
        working_directory=Path("/tmp"),
        cli_path="/bin/claude",
        model="claude-opus-test",
    )

    session = await service.spawn(
        task="implement it",
        chat_id=123,
        output_callback=AsyncMock(),
        model=None,
    )

    assert session.model == "claude-opus-test"
    start.assert_awaited_once()
