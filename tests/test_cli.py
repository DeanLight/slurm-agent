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


def test_task_new_prints_only_the_id_on_stdout(tmp_path):
    """`TASK=$(poe task-new "...")` must capture the id and nothing else.

    This is the whole contract: an id has to cross a shell boundary to reach the agent's
    prompt, so anything friendly printed on stdout — "opened TASK-118", a cost, a blank
    line — silently becomes part of the task id and the agent is launched against garbage.
    So this runs the real command with a stub `claude` on PATH and reads the two streams
    apart, rather than trusting that the print statements are where they look.
    """
    import json
    import os
    import subprocess

    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\nexec cat <<'EOF'\n" + json.dumps(
        {"result": "Created TASK-118 for you.", "total_cost_usd": 0.042,
         "is_error": False}) + "\nEOF\n")
    stub.chmod(0o755)

    done = subprocess.run(
        ["slurm-agent", "task-new", "Add a retry to the loader"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert done.returncode == 0, done.stderr
    # Exactly the id. Not a line containing it.
    assert done.stdout.strip() == "TASK-118"
    assert done.stdout.count("\n") == 1
    # The human-readable part went to stderr, where `$( )` cannot swallow it.
    assert "opened TASK-118" in done.stderr and "0.042" in done.stderr


def test_task_new_fails_loudly_rather_than_printing_something_unusable(tmp_path):
    """Two ids, or none, must not reach a launch. A wrong task is worse than no task."""
    import json
    import os
    import subprocess

    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\nexec cat <<'EOF'\n" + json.dumps(
        {"result": "Created TASK-118 and TASK-119.", "total_cost_usd": 0.01,
         "is_error": False}) + "\nEOF\n")
    stub.chmod(0o755)

    done = subprocess.run(
        ["slurm-agent", "task-new", "two rows by mistake"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert done.returncode != 0
    assert done.stdout.strip() == "", "nothing may reach stdout when the id is ambiguous"
    assert "exactly one task id" in done.stderr
