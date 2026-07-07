"""``run_gh`` — the injectable async gh runner (spec 02 §Dependencies).

Every ``gh`` call is an arg-list to ``asyncio.create_subprocess_exec`` — never a
shell string — so nothing the utterance carries can be interpreted as shell. The
subprocess is monkeypatched here so the test never touches GitHub.
"""

from __future__ import annotations

import asyncio

import pytest

from edith.skills.gh import GhError, run_gh


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


async def test_run_gh_builds_arg_list_and_returns_stdout(monkeypatch) -> None:  # noqa: ANN001
    seen: dict[str, object] = {}

    async def fake_exec(program, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen["program"] = program
        seen["args"] = args
        return FakeProcess(b"hello\n", b"", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    out = await run_gh(["pr", "list", "--repo", "patterninc/edith"])

    assert seen["program"] == "gh"  # program is gh, args are the list — never a shell string
    assert seen["args"] == ("pr", "list", "--repo", "patterninc/edith")
    assert out == "hello\n"


async def test_run_gh_raises_gherror_on_nonzero_exit(monkeypatch) -> None:  # noqa: ANN001
    async def fake_exec(program, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return FakeProcess(b"", b"not found\n", 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(GhError, match="not found"):
        await run_gh(["pr", "view", "999"])
