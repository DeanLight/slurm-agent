"""The `slurm-agent` command line. Every capability in this repo is reachable here.

Each command is a thin call into the modules that do the work, so `poe --help` and
`slurm-agent --help` are the same inventory and an agent driving this repo works through
the same surface a human does.

Commands land layer by layer; the ones not yet implemented raise `NotImplementedError`
naming the stack layer that brings them, so the inventory is complete from the start.
"""

import cyclopts

app = cyclopts.App(name="slurm-agent", help="Drive Tillicum jobs and remote Claude agents.")


def _pending(layer: str) -> None:
    raise NotImplementedError(f"lands in stack layer {layer}")


# ── setup ────────────────────────────────────────────────────────────────────────
@app.command
def init(send: bool = True) -> None:
    """Create the local footprint, then run a full healthcheck."""
    _pending("10-init-skills-docs")


@app.command
def healthcheck(full: bool = False, send: bool = False) -> None:
    """Is everything wired and working? Fast by default; `--full` adds the slow proofs."""
    _pending("10-init-skills-docs")


# ── allocations ──────────────────────────────────────────────────────────────────
@app.command(name="job-up")
def job_up(name: str, gpus: int = 1, time: str = "04:00:00", qos: str | None = None,
           cpus: int = 8, mem: str = "200G") -> None:
    """Bring up an allocation, or return the one already running under that name."""
    _pending("02-jobs")


@app.command(name="job-status")
def job_status() -> None:
    """Every allocation of mine, one line each."""
    _pending("02-jobs")


@app.command(name="job-shell")
def job_shell(name: str) -> None:
    """Open an interactive shell on the allocation's compute node."""
    _pending("02-jobs")


@app.command(name="job-down")
def job_down(name: str) -> None:
    """Cancel the allocation."""
    _pending("02-jobs")


# ── remote agents ────────────────────────────────────────────────────────────────
@app.command(name="agent-run")
def agent_run(task: str, job: str, agent: str, exp_id: str | None = None) -> None:
    """Stage a repo and launch a Claude agent on an allocation."""
    _pending("04-launch")


@app.command(name="agent-batch")
def agent_batch(task: str, agent: str, time: str | None = None,
                exp_id: str | None = None) -> None:
    """Submit the same agent as a self-terminating sbatch job."""
    _pending("08-batch")


@app.command(name="agent-status")
def agent_status() -> None:
    """One line per live remote agent, from the polled status block."""
    _pending("06-supervise")


@app.command(name="agent-logs")
def agent_logs(session: str, cells: bool = False, tail: int = 50) -> None:
    """Read a remote agent's notebook or log without copying it back."""
    _pending("04-launch")


@app.command(name="agent-watch")
def agent_watch(once: bool = False, auto_renew: bool = False) -> None:
    """The supervision loop: poll, decide, act, log."""
    _pending("06-supervise")


@app.command(name="agent-kill")
def agent_kill(session: str, reason: str) -> None:
    """Stop one agent, recording the reason the way an automatic kill is recorded."""
    _pending("06-supervise")


@app.command(name="agent-continue")
def agent_continue(session: str, job: str | None = None) -> None:
    """A fresh lease on the same notebook: done rounds skip."""
    _pending("06-supervise")


# ── polling and tidying ──────────────────────────────────────────────────────────
@app.command
def status(older_than: str = "14d") -> None:
    """What is running, queued, completed and failed."""
    _pending("07-status")


@app.command
def flush(older_than: str = "7d", failed: bool = False, session: str | None = None,
          dry_run: bool = False) -> None:
    """Drop finished runs from `status` by pruning their run roots."""
    _pending("07-status")


# ── notifications and usage ──────────────────────────────────────────────────────
@app.command(name="notify-test")
def notify_test() -> None:
    """Really send one message per channel, from here and from the cluster."""
    _pending("05-notify")


@app.command(name="monitor-run")
def monitor_run(dry_run: bool = False) -> None:
    """Poll usage and send the digest, but only if spend actually moved."""
    _pending("09-monitor")


@app.command(name="monitor-install")
def monitor_install() -> None:
    """Install the usage-digest schedule on this machine."""
    _pending("09-monitor")


@app.command(name="monitor-status")
def monitor_status() -> None:
    """Is the schedule on, when did it last fire, when does it fire next."""
    _pending("09-monitor")


@app.command(name="monitor-uninstall")
def monitor_uninstall() -> None:
    """Remove the schedule. Idempotent, like the install."""
    _pending("09-monitor")


@app.command(name="session-new")
def session_new(name: str) -> None:
    """Scaffold a session artifact notebook from the template."""
    _pending("10-init-skills-docs")


def main() -> None:
    app()
