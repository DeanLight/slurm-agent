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
# Tasks that start the MANAGER rather than drive the cluster. They cannot be cyclopts
# commands: each one `exec`s an interactive `claude`, and a package that shells out to a
# TUI is precisely what the smoke suite's failing runner factories exist to prevent — a
# `slurm-agent claude-manage` would replace the test process mid-run. They also configure
# nothing, so `poe --help` is still the honest inventory: what they add is the Remote
# Control name and `--continue`, which live on a command line and cannot be checked in.
LAUNCHER_TASKS = {"claude-manage", "manage"}


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
    extra = _poe_tasks() - _cli_commands() - TEMPLATE_TASKS - LAUNCHER_TASKS
    assert not extra, f"poe tasks with no CLI command: {sorted(extra)}"
