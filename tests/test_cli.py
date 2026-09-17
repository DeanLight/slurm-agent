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
# A poe task that opens an interactive program has to be a `cmd` task. Poe runs a SHELL
# task by feeding the script body to the interpreter on stdin, so the child's stdin is a
# pipe: `exec claude` inherits it, finds no terminal to read from, and silently gives you
# something that is not a conversation. `manage` was written as a shell task first, and
# this is what it cost.
INTERACTIVE_TASKS = {"manage", "job-shell"}


def _poe_config() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["poe"]["tasks"]


def _poe_tasks() -> set[str]:
    return set(_poe_config())


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


def test_a_task_that_opens_a_terminal_program_is_a_cmd_task():
    """Not a `shell` task, whose child gets a pipe on stdin and cannot be interactive."""
    tasks = _poe_config()
    for name in INTERACTIVE_TASKS:
        task = tasks[name]
        while isinstance(task, dict) and "ref" in task:
            task = tasks[task["ref"].split()[0]]
        assert isinstance(task, dict) and "cmd" in task, (
            f"`poe {name}` opens an interactive program, so it must be a cmd task — "
            "a poe shell task hands its child a pipe on stdin and the session dies silently"
        )
