"""Run every command, end to end, against a fake cluster.

The unit tests exercise functions with values a test constructed. That is why three bugs
reached a real laptop instead of failing here: a config the models no longer accepted, a
log line on stdout, and an unquoted `|` the remote shell read as a pipe. Each needed
something to *run the command* — the real entry point, the real argument parsing, the real
committed config — and nothing did.

So this does. Every `slurm-agent` command runs through `cli.app`, with the two seams that
reach outside the process replaced by fakes: the cluster (ssh) and the laptop (subprocess).
Everything else is the real thing, including `config/*.yaml` and `agents/*.yaml` as
committed.

It asserts almost nothing about behaviour on purpose. Its job is that the command runs at
all — which is precisely the class of bug that has been escaping.
"""

import json
import os
from pathlib import Path

import pytest

from slurm_agent import cli
from tests.fake_cluster import FakeCluster

ROOT = Path(__file__).resolve().parent.parent

# Every command, with arguments that make sense against the fake. Keep this list complete:
# a command missing from it is a command nobody runs until a user does.
COMMANDS: list[list[str]] = [
    ["healthcheck"],
    ["healthcheck", "--full"],
    ["job-up", "dev"],
    ["job-status"],
    ["job-down", "dev"],
    ["agent-run", "TASK-118", "--job", "dev", "--agent", "smoke"],
    ["agent-batch", "TASK-118", "--agent", "smoke"],
    ["agent-status"],
    ["agent-logs", "4f2c"],
    ["agent-logs", "4f2c", "--cells"],
    ["agent-watch", "--once"],
    ["agent-continue", "4f2c"],
    ["agent-kill", "4f2c", "--reason", "wrong config"],
    ["status"],
    ["flush", "--dry-run"],
    ["monitor-run", "--dry-run"],
    ["monitor-status"],
]


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    """A fake Tillicum and a fake laptop, wired into the seams that leave this process.

    Everything the CLI reads from disk stays real — `config/*.yaml` and `agents/*.yaml` as
    committed — because a stale config is one of the bugs this is here to catch. What is
    replaced is only what would otherwise need a cluster, a credential, or a crontab.

    Nothing here may reach a real process. A smoke suite that quietly shells out is slow,
    fails offline, depends on who is logged in, and — with `claude -p` in the `--full`
    tier — spends money every run. Both runner factories are replaced with something that
    FAILS, so an escape is loud: that is how the first version of this fixture was found to
    be running `git ls-remote` and `claude -p` for real, having patched `local_runner` in
    the module that defines it rather than the one that imports it.
    """
    from slurm_agent.config import ClusterConfig, ManagerConfig, load

    monkeypatch.chdir(ROOT)
    fake = FakeCluster()
    monkeypatch.setattr(cli, "_runner", lambda: fake)
    # `preflight` imports `local_runner` by name, so patching it in `remote` would miss —
    # and missing is not loud: the suite quietly ran `git ls-remote` and `claude -p` for
    # real, over the network and against a token budget. Patch where it is USED, and then
    # make the mistake impossible to repeat by cutting the subprocess call underneath.
    monkeypatch.setattr("slurm_agent.preflight.local_runner", lambda **kw: fake)
    def _escaped(*a, **kw):
        pytest.fail(f"a smoke run built a real runner: {a}, {kw}")

    monkeypatch.setattr("slurm_agent.remote.ssh_runner", _escaped)
    monkeypatch.setattr("slurm_agent.remote.local_runner", _escaped)

    # A filled .envrc, so `hc` answers about keys rather than about a missing file.
    envrc = tmp_path / ".envrc"
    envrc.write_text("SLURM_AGENT_SMTP_HOST=h\n")
    envrc.chmod(0o600)
    for key, value in (("SLURM_AGENT_SMTP_HOST", "h"), ("SLURM_AGENT_SMTP_USER", "u"),
                       ("SLURM_AGENT_SMTP_PASSWORD", "p")):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "_manager", lambda: ManagerConfig(envrc=envrc))

    # `job-up` rewrites the node's Hostname into a real local file. Point it at a scratch
    # one rather than the developer's actual ssh config.
    node_config = tmp_path / "tillicum-node-config"
    node_config.write_text("Host tillicum-node\n    Hostname placeholder\n")
    real_cluster = load(ROOT / "config" / "cluster.yaml", ClusterConfig)
    monkeypatch.setattr(
        cli, "_cluster",
        lambda: real_cluster.model_copy(update={"node_config_path": node_config}))

    # A crontab that exists and does nothing, so `monitor-*` is exercised rather than
    # skipped on a machine that happens not to have one.
    crontab = tmp_path / "crontab"
    crontab.write_text("#!/bin/sh\nexit 0\n")
    crontab.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return fake


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
def test_every_command_runs(argv, cluster):
    """It executes and does not raise. That is the whole assertion, and it is enough.

    `SystemExit` is fine — `healthcheck` exits non-zero when a row is MISSING, which is the
    command working. Anything else is the class of bug that has been escaping to a laptop:
    a config the models no longer accept, a name that moved, an f-string that is wrong only
    on the path nothing exercised.

    Every command the fake is handed is syntax-checked as it arrives, so this also proves
    nothing it would send could be rejected by a remote shell.
    """
    try:
        cli.app(argv)
    except SystemExit:
        pass


def test_the_command_list_is_complete():
    """A command nobody smoke-tests is a command nobody runs until a user does."""
    covered = {argv[0] for argv in COMMANDS}
    # Exempt because each does something a smoke run must not: `init` writes into the
    # developer's ~/.ssh, `notify-test` really sends, `monitor-install`/`-uninstall` edit a
    # live crontab, and `session-new` takes a name and writes a file.
    exempt = {"init", "notify-test", "session-new", "monitor-install", "monitor-uninstall",
              "job-shell"}
    every = {n for name in cli.app for n in ([name] if isinstance(name, str) else name)}
    every = {n for n in every if not n.startswith("-")}
    assert every - covered - exempt == set(), \
        f"not smoke-tested: {sorted(every - covered - exempt)}"


def test_nothing_any_command_sends_would_break_a_remote_shell(cluster):
    """Run them all, then say what that proved.

    `FakeCluster` runs `bash -n` over every command as it arrives, so reaching the end IS
    the assertion — this names it because the invariant is worth naming. Anything with
    shell meaning must be quoted before it crosses ssh, and a literal `|` in a format
    string is the way that has actually gone wrong on a real cluster.
    """
    for argv in COMMANDS:
        try:
            cli.app(argv)
        except SystemExit:
            pass
    assert len(cluster.commands) > 20, "this proved less than it looks like it did"


def test_a_launch_record_says_where_the_agent_is_and_what_it_may_do(cluster):
    """One behavioural check, because a launch is what costs money if it is wrong."""
    try:
        cli.app(["agent-run", "TASK-118", "--job", "dev", "--agent", "smoke"])
    except SystemExit:
        pass

    # Match the redirect TARGET, not the body: `remote_notify.py` mentions launch.json in
    # its own docstring, and is written to the run root by the same heredoc idiom.
    written = [c for c in cluster.commands
               if c.startswith("cat > ") and c.split(" <<", 1)[0].endswith("launch.json")]
    assert written, "a launch must record what it did"
    body = written[0].split("<<'SLURM_AGENT_EOF'\n", 1)[1].rsplit("\nSLURM_AGENT_EOF", 1)[0]
    record = json.loads(body)
    assert record["task"] == "TASK-118"
    # The trial agents write beside the launch record, never into the staged repo.
    assert not record["log_dir"].startswith(record["workdir"])

    step = [c for c in cluster.commands if "setsid nohup srun" in c]
    assert step and "--overlap" in step[0], "agents share one allocation as steps"
    assert "--strict-mcp-config" not in step[0] or "--mcp-config" in step[0]


def test_job_shell_builds_an_ssh_command_it_can_hand_over_to(cluster):
    """`job-shell` becomes the shell with `execvp`, so it cannot be run in-process.

    What is worth checking is the argv it would hand over: it is built from the live queue,
    so a node that moved or a job that is not running has to show up here rather than as a
    shell that opens somewhere unexpected.
    """
    from slurm_agent import jobs

    argv = jobs.job_shell_command("dev", cluster, cli._cluster())
    assert argv[0] == "ssh"
    assert any("tillicum" in part for part in argv), argv


def test_the_runner_is_the_only_way_out_of_this_process():
    """One seam, enforced. Anything that shells out around it is untestable from here.

    `remote.py` owns `subprocess`, and `monitor.py` owns `crontab` because a crontab is
    local by nature. Anywhere else, a direct `subprocess` call would be a path this smoke
    suite cannot fake — which means a path nothing checks until it runs on a laptop.
    """
    allowed = {"remote.py", "monitor.py"}
    offenders = []
    for path in (ROOT / "slurm_agent").glob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text()
        if "subprocess" in text and "import subprocess" in text:
            offenders.append(path.name)
    assert not offenders, f"these bypass the Runner seam: {offenders}"
