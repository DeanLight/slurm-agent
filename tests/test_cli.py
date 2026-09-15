"""The CLI and the poe inventory must not drift apart.

`poe --help` is meant to be the inventory of what this repo can do. That only holds if
every cyclopts command has a poe task and vice versa — a command added to one and not the
other is invisible to half its audience, and nothing else would catch it.
"""

import tomllib
from pathlib import Path

from slurm_agent.cli import app

ROOT = Path(__file__).resolve().parent.parent
# poe tasks that are not slurm-agent commands: notebook plumbing from the juplit template.
TEMPLATE_TASKS = {
    "hooks", "sync", "nb", "clean", "test", "check", "html", "skill",
    "docs", "docs-build", "docs-deploy", "hc",
}


def _poe_tasks() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return set(config["tool"]["poe"]["tasks"])


def _cli_commands() -> set[str]:
    names = {n for name in app for n in ([name] if isinstance(name, str) else name)}
    # cyclopts registers --help/--version itself; they are not repo capabilities.
    return {n for n in names if not n.startswith("-")}


def test_every_cli_command_has_a_poe_task():
    missing = _cli_commands() - _poe_tasks()
    assert not missing, f"CLI commands with no poe task: {sorted(missing)}"


def test_every_poe_task_is_a_cli_command_or_template_plumbing():
    extra = _poe_tasks() - _cli_commands() - TEMPLATE_TASKS
    assert not extra, f"poe tasks with no CLI command: {sorted(extra)}"


def _stub_claude(tmp_path, payload):
    """A fake `claude` on PATH that answers with one JSON object."""
    import json

    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\nexec cat <<'EOF'\n" + json.dumps(payload) + "\nEOF\n")
    stub.chmod(0o755)
    return stub


def _run_cli(tmp_path, *args):
    import os
    import subprocess

    return subprocess.run(["slurm-agent", *args], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})


def test_ask_puts_only_the_reply_on_stdout(tmp_path):
    """`IDS=$(poe ask "…")` must capture the manager's answer and nothing else.

    This is the whole contract: the answer crosses a shell boundary to become the input of
    the next ask. Anything friendly on stdout — a cost, a session id, a log line — silently
    becomes part of it, and the next session is asked to run a task named after a timestamp.
    So this runs the real command with a stub `claude` on PATH and reads the two streams
    apart, rather than trusting that the prints are where they look.
    """
    _stub_claude(tmp_path, {"result": "TASK-118\nTASK-119", "total_cost_usd": 0.21,
                            "session_id": "abc123", "is_error": False})
    done = _run_cli(tmp_path, "ask", "Open two tasks")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "TASK-118\nTASK-119\n"
    # Everything else went to stderr, including the log line structlog would have put on
    # stdout by default.
    assert "abc123" in done.stderr and "0.210" in done.stderr
    assert "manager.replied" in done.stderr


def test_ask_carries_the_repo_context_into_the_session(tmp_path):
    """A manager that has not read its own skill invents a worse procedure, invisibly."""
    _stub_claude(tmp_path, {"result": "ok", "total_cost_usd": 0.0, "is_error": False})
    done = _run_cli(tmp_path, "ask", "MARKER-PROMPT")

    assert done.returncode == 0, done.stderr
    # The stub echoes nothing back, so assert on what the real argv would carry instead.
    from slurm_agent.config import ManagerConfig
    from slurm_agent.manager import manager_argv

    argv = manager_argv(ManagerConfig(), "MARKER-PROMPT")
    assert "slurm-orchestration/SKILL.md" in argv[2] and "CLAUDE.md" in argv[2]
    assert argv[2].endswith("MARKER-PROMPT")
    assert "--bare" not in argv


def test_ask_fails_loudly_rather_than_printing_an_error_as_an_answer(tmp_path):
    """An errored session's `result` is a message, not an answer. It must not reach stdout."""
    _stub_claude(tmp_path, {"result": "budget exceeded", "is_error": True,
                            "total_cost_usd": 5.0})
    done = _run_cli(tmp_path, "ask", "Open two tasks")

    assert done.returncode != 0
    assert done.stdout.strip() == "", "an error must never look like the manager's reply"
    assert "the manager session failed" in done.stderr
