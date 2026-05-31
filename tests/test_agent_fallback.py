"""Headless tests for AgentFallbackConnector — no real `claude`, no display.

The spawn boundary (`_spawn`) is monkeypatched so no real agent runs; the tasks
root is injected into a tmp dir so nothing is written under $HOME. We assert the
catch-all confidence floor, the exact argv + workdir construction, and that
execute() never raises and maps exit codes correctly.
"""
from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

from curby_jarvis.connectors.agent_fallback import AgentFallbackConnector
from curby_jarvis.intent import RISK_AMBIGUOUS, ConnectorResult, Intent


def conn(tmp_path, timeout=5.0) -> AgentFallbackConnector:
    return AgentFallbackConnector(tasks_root=str(tmp_path), timeout=timeout)


# -- contract: cost / name ---------------------------------------------------

def test_cost_is_ten():
    c = AgentFallbackConnector()
    assert c.cost == 10
    assert c.name == "agent_fallback"


def test_is_available_always_true():
    assert AgentFallbackConnector().is_available(Intent("agent_task")) is True
    assert AgentFallbackConnector().is_available(Intent("frobnicate")) is True


# -- can_handle: floor > 0 for ANY verb so the chain always terminates -------

def test_agent_task_full_confidence():
    assert AgentFallbackConnector().can_handle(Intent("agent_task")) == 1.0


@pytest.mark.parametrize("verb", [
    "open", "play", "close", "click_at", "type", "drag", "totally_unknown_verb", "",
])
def test_catchall_floor_positive_for_any_verb(verb):
    # The whole point: a small but strictly-positive confidence for arbitrary
    # verbs so the router's candidate filter (conf > 0.0) always keeps it.
    score = AgentFallbackConnector().can_handle(Intent(verb))
    assert 0.0 < score < 1.0


# -- preview: always-confirm amber agent card --------------------------------

def test_preview_is_ambiguous_and_audits_argv():
    monkey_env = dict(os.environ)
    monkey_env["CLAUDE_CLI"] = "/usr/local/bin/claude"
    os.environ.update(CLAUDE_CLI="/usr/local/bin/claude")
    try:
        card = AgentFallbackConnector().preview(
            Intent("agent_task", raw_utterance="refactor my dotfiles"))
        assert card.title == "agent task"
        assert card.risk == RISK_AMBIGUOUS
        assert card.gloss == "refactor my dotfiles"
        # literal carries the exact command for audit
        assert "claude" in card.literal
        assert "-p" in card.literal
        assert "--dangerously-skip-permissions" in card.literal
        assert "refactor my dotfiles" in card.literal
    finally:
        os.environ.pop("CLAUDE_CLI", None)


# -- argv construction: exact, honoring CLAUDE_CLI override ------------------

def test_build_argv_uses_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI", "/opt/claude-bin")
    argv = AgentFallbackConnector()._build_argv("do the thing")
    assert argv == ["/opt/claude-bin", "-p", "--dangerously-skip-permissions", "do the thing"]


def test_build_argv_falls_back_to_path_name(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI", raising=False)
    # Force shutil.which to miss -> bare "claude" name (spawn surfaces the error).
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = AgentFallbackConnector()._build_argv("hello")
    assert argv == ["claude", "-p", "--dangerously-skip-permissions", "hello"]


# -- workdir: fresh ~/curby-jarvis-tasks/<ts>-<slug>/ ------------------------

def test_make_workdir_shape(tmp_path):
    c = conn(tmp_path)
    wd = c._make_workdir("Open Spotify And Play!")
    assert os.path.isdir(wd)
    assert wd.startswith(str(tmp_path))
    base = os.path.basename(wd)
    # <ts>-<slug>: timestamp prefix then a filesystem-safe slug
    assert "-" in base
    assert base.endswith("open-spotify-and-play")


def test_slug_is_filesystem_safe(tmp_path):
    wd = conn(tmp_path)._make_workdir("!!! @#$ %^&")
    # all-symbol utterance collapses to the default slug, dir still created
    assert os.path.basename(wd).endswith("task")
    assert os.path.isdir(wd)


# -- execute: exit 0 -> ok, builds right argv + workdir, never raises --------

def test_execute_exit_zero_is_ok(tmp_path, monkeypatch):
    c = conn(tmp_path)
    captured = {}

    def fake_spawn(argv, workdir):
        captured["argv"] = argv
        captured["workdir"] = workdir
        return 0

    monkeypatch.setattr(c, "_spawn", fake_spawn)
    res = c.execute(Intent("agent_task", raw_utterance="summarize my inbox"))
    assert isinstance(res, ConnectorResult)
    assert res.ok is True
    assert res.mechanism == "agent_fallback"
    # right argv
    assert captured["argv"][1:] == ["-p", "--dangerously-skip-permissions", "summarize my inbox"]
    # right workdir: under injected root, fresh, exists
    assert captured["workdir"].startswith(str(tmp_path))
    assert os.path.isdir(captured["workdir"])
    assert captured["workdir"].endswith("summarize-my-inbox")


def test_execute_nonzero_exit_not_ok(tmp_path, monkeypatch):
    c = conn(tmp_path)
    monkeypatch.setattr(c, "_spawn", lambda argv, workdir: 3)
    res = c.execute(Intent("agent_task", raw_utterance="do something"))
    assert res.ok is False
    assert res.error == "agent_failed"
    assert "exit=3" in res.detail


def test_execute_timeout_not_ok(tmp_path, monkeypatch):
    c = conn(tmp_path)
    monkeypatch.setattr(c, "_spawn", lambda argv, workdir: None)
    res = c.execute(Intent("agent_task", raw_utterance="long running task"))
    assert res.ok is False
    assert res.error == "agent_timeout"


def test_execute_empty_utterance_fails_fast(tmp_path):
    res = conn(tmp_path).execute(Intent("agent_task", raw_utterance=""))
    assert res.ok is False
    assert res.error == "empty_utterance"


def test_execute_falls_back_to_target_when_no_raw(tmp_path, monkeypatch):
    c = conn(tmp_path)
    captured = {}
    monkeypatch.setattr(c, "_spawn", lambda argv, workdir: captured.setdefault("argv", argv) and 0 or 0)
    res = c.execute(Intent("agent_task", target="fix the build"))
    assert res.ok is True
    assert captured["argv"][-1] == "fix the build"


def test_execute_never_raises(tmp_path, monkeypatch):
    c = conn(tmp_path)

    def boom(argv, workdir):
        raise RuntimeError("popen wedged")

    monkeypatch.setattr(c, "_spawn", boom)
    res = c.execute(Intent("agent_task", raw_utterance="anything"))
    assert res.ok is False
    assert res.error == "exception"


# -- _spawn body: exercise real argv plumbing with a fake subprocess ---------

def test_spawn_uses_detached_session(tmp_path, monkeypatch):
    """Stub the lazy `subprocess` module so _spawn's real body runs headless and
    we assert it spawns detached (start_new_session) in the given workdir."""
    calls = {}

    class FakeProc:
        def wait(self, timeout=None):
            calls["waited"] = timeout
            return 0

    class FakeTimeoutExpired(Exception):
        pass

    def fake_popen(argv, cwd=None, start_new_session=None, stdin=None):
        calls["argv"] = argv
        calls["cwd"] = cwd
        calls["detached"] = start_new_session
        return FakeProc()

    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.Popen = fake_popen
    fake_subprocess.DEVNULL = -3
    fake_subprocess.TimeoutExpired = FakeTimeoutExpired
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)

    c = conn(tmp_path, timeout=42.0)
    code = c._spawn(["claude", "-p", "--dangerously-skip-permissions", "x"], str(tmp_path))
    assert code == 0
    assert calls["detached"] is True
    assert calls["cwd"] == str(tmp_path)
    assert calls["waited"] == 42.0
    assert calls["argv"][0] == "claude"


def test_spawn_timeout_returns_none(tmp_path, monkeypatch):
    class FakeTimeoutExpired(Exception):
        pass

    class FakeProc:
        def wait(self, timeout=None):
            raise FakeTimeoutExpired()

    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.Popen = lambda *a, **k: FakeProc()
    fake_subprocess.DEVNULL = -3
    fake_subprocess.TimeoutExpired = FakeTimeoutExpired
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)

    code = conn(tmp_path)._spawn(["claude"], str(tmp_path))
    assert code is None


# -- headless import ---------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.connectors.agent_fallback")
    assert hasattr(m, "AgentFallbackConnector")
