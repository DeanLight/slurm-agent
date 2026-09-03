"""The `slurm-agent` command line. Every capability in this repo is reachable here.

Each command is a thin call into the modules that do the work, so `poe --help` and
`slurm-agent --help` are the same inventory and an agent driving this repo works through
the same surface a human does.

Commands land layer by layer; the ones not yet implemented raise `NotImplementedError`
naming the stack layer that brings them, so the inventory is complete from the start.
"""

import os

import cyclopts

from slurm_agent.config import (
    AgentConfig,
    ClusterConfig,
    ManagerConfig,
    MonitorConfig,
    SupervisionConfig,
    declared_env_keys,
    load,
)

app = cyclopts.App(name="slurm-agent", help="Drive Tillicum jobs and remote Claude agents.")


def _pending(layer: str) -> None:
    raise NotImplementedError(f"lands in stack layer {layer}")


def _cluster() -> ClusterConfig:
    return load("config/cluster.yaml", ClusterConfig)


def _runner():
    from slurm_agent.remote import ssh_runner

    return ssh_runner(_cluster().login_host)


LEDGER = "ledger.jsonl"


def _notifier():
    """A `send(subject, body)` bound to the configured channels, or None if unconfigured."""
    from slurm_agent.notify import NotifyConfig, notify

    cfg = load("config/notify.yaml", NotifyConfig)
    keys = declared_env_keys(load("config/manager.yaml", ManagerConfig), [])
    return lambda subject, body: notify(subject, body, cfg, keys)


def _agent(kind: str) -> AgentConfig:
    return load(f"agents/{kind}.yaml", AgentConfig)


def _views():
    from slurm_agent import watch as supervisor
    from slurm_agent.remote import probe

    cluster, run = _cluster(), _runner()
    snapshot = probe(run, cluster.run_root)
    return snapshot, supervisor.views(snapshot, cluster), cluster, run


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
    from slurm_agent import jobs

    cluster = _cluster()
    job = jobs.job_up(name, _runner(), cluster, gpus=gpus, time_limit=time, qos=qos,
                      cpus=cpus, mem=mem)
    left = job.time_left_s and f"{job.time_left_s // 3600}:{job.time_left_s % 3600 // 60:02d} left"
    print(f"job {job.job_id} on {job.node or 'pending'} · {job.state} · {left or '—'} "
          f"· {job.gpus} gpu · est. ${job.gpu_usd(cluster):.2f} so far")


@app.command(name="job-status")
def job_status() -> None:
    """Every allocation of mine, one line each."""
    from slurm_agent import jobs

    cluster = _cluster()
    rows = jobs.job_list(_runner())
    if not rows:
        print("no allocations")
        return
    for job in rows:
        left = f"{job.time_left_s // 60} min left" if job.time_left_s else "—"
        kind = "batch" if job.batch else "interactive"
        print(f"{job.name:<14} {job.job_id:<8} {job.node or '-':<6} {job.state:<3} "
              f"{left:<14} {job.gpus} gpu  ${job.gpu_usd(cluster):.2f}  {kind}")


@app.command(name="job-shell")
def job_shell(name: str) -> None:
    """Open an interactive shell on the allocation's compute node."""
    from slurm_agent import jobs

    argv = jobs.job_shell_command(name, _runner(), _cluster())
    os.execvp(argv[0], argv)          # become the shell rather than wrapping it


@app.command(name="job-down")
def job_down(name: str) -> None:
    """Cancel the allocation."""
    from slurm_agent import jobs

    print(f"cancelled job {jobs.job_down(name, _runner())} ({name})")


# ── remote agents ────────────────────────────────────────────────────────────────
@app.command(name="agent-run")
def agent_run(task: str, job: str, agent: str, exp_id: str | None = None) -> None:
    """Stage a repo and launch a Claude agent on an allocation."""
    from slurm_agent import launch as launcher

    cfg = _agent(agent)
    session = launcher.launch(cfg, task, job, _runner(), _cluster(), exp_id=exp_id)
    print(f"launched agent {agent!r} for {task} · session {session}")
    print(f"lease {cfg.lease} · budget ${cfg.max_budget_usd} · up to {cfg.max_leases} leases")


@app.command(name="agent-batch")
def agent_batch(task: str, agent: str, time: str | None = None,
                exp_id: str | None = None, gpus: int = 1) -> None:
    """Submit the same agent as a self-terminating sbatch job."""
    from slurm_agent import launch as launcher

    cfg = _agent(agent)
    session, job_id = launcher.launch_batch(cfg, task, _runner(), _cluster(),
                                            exp_id=exp_id, time_limit=time, gpus=gpus)
    print(f"submitted batch job {job_id} · session {session}")
    print(f"walltime {time or cfg.batch_time} · budget ${cfg.max_budget_usd} · "
          "ends by itself when the agent exits")


@app.command(name="agent-status")
def agent_status() -> None:
    """One line per live remote agent, from the polled status block."""
    _, rows, _, _ = _views()
    if not rows:
        print("no remote agents")
        return
    for v in rows:
        left = f"{(v.time_left_s or 0) // 60}m left" if v.time_left_s else "—"
        spend = f"${v.gpu_usd:.2f} gpu" + (f" · ${v.agent_usd:.2f} agent" if v.agent_usd else "")
        waiting = f" · waiting on {', '.join(v.waiting_on)}" if v.waiting_on else ""
        print(f"{v.session_id[:4]}  {v.task:<10} {v.state:<11} "
              f"round {v.round or '—':<5} {left:<10} {spend}{waiting}")


@app.command(name="agent-logs")
def agent_logs(session: str, cells: bool = False, tail: int = 50) -> None:
    """Read a remote agent's notebook or log without copying it back."""
    import json

    from slurm_agent.remote import quote, remote_path

    cluster, run = _cluster(), _runner()
    run_dir = remote_path(f"{cluster.run_root.rstrip('/')}/{session}")
    if cells:
        # `juplit cells` runs on the LOGIN NODE over the shared filesystem, so a 4 MB
        # notebook costs a few hundred tokens and is never copied to the laptop.
        record = json.loads(run(f"cat {run_dir}/launch.json"))
        print(run(f"juplit cells {quote(record['notebook'])}"))
    else:
        print(run(f"tail -n {int(tail)} {run_dir}/agent.log {run_dir}/agent.err"))


@app.command(name="agent-watch")
def agent_watch(once: bool = False, auto_renew: bool = False) -> None:
    """The supervision loop: poll, decide, act, log."""
    from slurm_agent import watch as supervisor

    supervisor.watch(_runner(), _cluster(), load("config/supervision.yaml", SupervisionConfig),
                     once=once, auto_renew=auto_renew)


@app.command(name="agent-kill")
def agent_kill(session: str, reason: str) -> None:
    """Stop one agent, recording the reason the way an automatic kill is recorded."""
    from slurm_agent import watch as supervisor

    _, rows, _, run = _views()
    match = next((v for v in rows if v.session_id.startswith(session)), None)
    if match is None:
        live = ", ".join(v.session_id[:8] for v in rows) or "none"
        raise SystemExit(f"no live session {session!r} — live sessions: {live}")
    target = supervisor.kill_step(match, run, reason=reason)
    print(f"killed {match.session_id[:8]} ({target or 'no step found'}) · {reason}")


@app.command(name="agent-continue")
def agent_continue(session: str, job: str | None = None) -> None:
    """A fresh lease on the same notebook: done rounds skip."""
    import json

    from slurm_agent import launch as launcher
    from slurm_agent.remote import remote_path

    cluster, run = _cluster(), _runner()
    run_dir = remote_path(f"{cluster.run_root.rstrip('/')}/{session}")
    record = json.loads(run(f"cat {run_dir}/launch.json"))
    cfg = _agent(record.get("agent_kind", "experiment-runner"))
    launcher.continue_run(session, job or record.get("job_name", ""), run, cluster, cfg)
    print(f"continued {session} · lease {record['leases_used'] + 1}/{record['max_leases']}")


# ── polling and tidying ──────────────────────────────────────────────────────────
@app.command
def status(older_than: str = "14d") -> None:
    """What is running, queued, completed and failed."""
    from slurm_agent import watch as supervisor

    snapshot, rows, cluster, _ = _views()
    jobs = supervisor._parse_queue(snapshot.get("queue", ""))
    print(supervisor.status_report(rows, supervisor.history(snapshot, cluster), jobs))


@app.command
def flush(older_than: str = "7d", failed: bool = False, session: str | None = None,
          dry_run: bool = False) -> None:
    """Drop finished runs from `status` by pruning their run roots."""
    from slurm_agent import watch as supervisor

    snapshot, _, cluster, run = _views()
    removed = supervisor.flush(snapshot, run, cluster, older_than=older_than,
                               failed=failed, session_id=session, dry_run=dry_run)
    if not removed:
        print("nothing to flush")
        return
    verb = "would remove" if dry_run else "removed"
    for path in removed:
        print(f"{verb} {path}")
    if not failed:
        print("failed runs kept — their agent.err is the only record of why they died")


# ── notifications and usage ──────────────────────────────────────────────────────
@app.command(name="notify-test")
def notify_test() -> None:
    """Really send one message per channel, from here and from the cluster."""
    from slurm_agent import notify as notifier
    from slurm_agent.config import ManagerConfig, declared_env_keys

    cfg = load("config/notify.yaml", notifier.NotifyConfig)
    manager = load("config/manager.yaml", ManagerConfig)
    rows = notifier.notify_test(cfg, declared_env_keys(manager, []), run=_runner())
    for where, ok, detail in rows:
        print(f"{where:<9} {'ok' if ok else 'FAILED':<7} {detail}")
    raise SystemExit(0 if all(ok for _, ok, _ in rows) else 1)


@app.command(name="monitor-run")
def monitor_run(dry_run: bool = False) -> None:
    """Poll usage and send the digest, but only if spend actually moved."""
    from slurm_agent import monitor

    cfg = load("config/monitor.yaml", MonitorConfig)
    send = None if dry_run else _notifier()
    print(monitor.monitor_run(_runner(), cfg, LEDGER, dry_run=dry_run, send=send))


@app.command(name="monitor-install")
def monitor_install() -> None:
    """Install the usage-digest schedule on this machine."""
    from pathlib import Path

    from slurm_agent import monitor

    cfg = load("config/monitor.yaml", MonitorConfig)
    line = monitor.cron_line(cfg, Path.cwd())
    monitor.cron_write(line)
    print(f"installed: {line}")


@app.command(name="monitor-status")
def monitor_status() -> None:
    """Is the schedule on, when did it last fire, when does it fire next."""
    from slurm_agent import monitor

    print(monitor.cron_status(LEDGER))


@app.command(name="monitor-uninstall")
def monitor_uninstall() -> None:
    """Remove the schedule. Idempotent, like the install."""
    from slurm_agent import monitor

    monitor.cron_write(None)
    print("removed: the slurm-agent monitor crontab block")


@app.command(name="session-new")
def session_new(name: str) -> None:
    """Scaffold a session artifact notebook from the template."""
    _pending("10-init-skills-docs")


def main() -> None:
    app()
