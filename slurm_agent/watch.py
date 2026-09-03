# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.16.0
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Supervision
#
# A poll loop with named thresholds, not a human happening to notice.
#
# `decide` is a **pure function** — `(view, rules, now) -> Decision` — which is the whole
# reason every threshold can be tested with no cluster and no clock. Everything expensive
# lives outside it.
#
# **Rules kill; the manager renews.** A kill is a threshold firing on evidence. A renewal
# needs somebody to read the log and the notebook, which a threshold cannot do — so it is
# proposed, not taken.

# %%
import json
import time
from typing import Literal

import structlog
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict

from slurm_agent.config import ClusterConfig, SupervisionConfig, duration_seconds
from slurm_agent.jobs import Job, job_list
from slurm_agent.remote import Runner, RemoteError, probe, quote, remote_path

log = structlog.get_logger(__name__)

TERMINAL = {"finished", "failed"}


# %%
class AgentView(BaseModel):
    """One remote agent, merged from launch.json, its status block, squeue and the files.

    Two progress signals on purpose. `status_age_s` is what the agent SAYS about itself;
    `notebook_age_s` is what the filesystem OBSERVED. A stuck agent fails one or the other,
    and a detector for "stuck" must not depend on the stuck agent's own account.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    task: str
    mode: Literal["interactive", "batch"] = "interactive"
    job_id: str
    job_state: Literal["RUNNING", "PENDING", "TIMEOUT", "FAILED", "GONE"]
    time_left_s: int | None = None
    state: Literal["running", "needs_env", "needs_human", "finished", "failed", "unknown"]
    round: str | None = None
    waiting_on: list[str] = []
    cells_done: int | None = None
    status_age_s: int | None = None
    notebook_age_s: int | None = None
    gpu_idle_s: int | None = None
    gpu_usd: float = 0.0
    agent_usd: float | None = None
    leases_used: int = 1
    max_leases: int = 4
    announced: bool = False
    run_dir: str = ""


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["watch", "kill", "renew", "escalate", "done"]
    rule: str | None = None
    detail: str


# %% [markdown]
# ## The policy
#
# Order is the policy. Terminal states first, then human-blocked (stop paying immediately —
# nobody is appearing in the next four hours), then the three staleness rules, then lease
# renewal, then keep watching.

# %%
def decide(view: AgentView, rules: SupervisionConfig, now: float) -> Decision:
    """The whole supervision policy, as a pure function. Every kill names its rule."""
    if view.state == "finished":
        if view.announced:
            return Decision(action="done", detail="finished; the agent announced itself")
        return Decision(action="escalate", detail="finished but never announced")
    if view.state == "failed":
        return Decision(action="escalate", detail="run failed")
    if view.job_state == "TIMEOUT":
        return Decision(action="escalate", detail="batch job timed out unfinished")
    if view.job_state in {"GONE", "FAILED"}:
        return Decision(action="escalate", detail="job vanished mid-run")

    if view.state in {"needs_env", "needs_human"}:
        blocked = duration_seconds(rules.blocked_for)
        if (view.status_age_s or 0) >= blocked:
            waiting = ", ".join(view.waiting_on) or "a human"
            return Decision(action="kill", rule=f"blocked_for={rules.blocked_for}",
                            detail=f"blocked on {waiting}")
        return Decision(action="watch", detail=f"blocked on {', '.join(view.waiting_on) or 'a human'}")

    for field, key in (("status_age_s", "status_stale_for"),
                       ("notebook_age_s", "no_progress_for"),
                       ("gpu_idle_s", "gpu_idle_for")):
        value = getattr(view, field)
        limit = getattr(rules, key)
        if value is not None and value >= duration_seconds(limit):
            return Decision(action="kill", rule=f"{key}={limit}",
                            detail=f"{field} {value}s over {limit}")

    if view.mode == "interactive" and view.time_left_s is not None:
        if view.time_left_s <= duration_seconds(rules.renew_when_time_left):
            if view.leases_used < view.max_leases:
                return Decision(action="renew", detail="lease ending")
            return Decision(action="escalate", detail="lease budget exhausted")

    left = f"{(view.time_left_s or 0) // 60}m left" if view.time_left_s else "no limit"
    return Decision(action="watch", detail=f"round {view.round or '—'} · {left}")


# %%
if test():
    rules = SupervisionConfig()

    def make(**kw) -> AgentView:
        base = dict(session_id="4f2c", task="TASK-104", job_id="62526",
                    job_state="RUNNING", state="running", time_left_s=7200)
        return AgentView(**{**base, **kw})

    # Terminal states, and the announce split that keeps the human's inbox quiet.
    assert decide(make(state="finished", announced=True), rules, 0).action == "done"
    assert decide(make(state="finished"), rules, 0).action == "escalate"
    assert decide(make(state="failed"), rules, 0).action == "escalate"
    assert decide(make(job_state="TIMEOUT"), rules, 0).action == "escalate"
    assert decide(make(job_state="GONE"), rules, 0).action == "escalate"

    # blocked_for is a boundary, not a hair trigger: a just-blocked run is watched.
    fresh_block = make(state="needs_env", waiting_on=["HF_TOKEN"], status_age_s=60)
    assert decide(fresh_block, rules, 0).action == "watch"
    stale_block = make(state="needs_env", waiting_on=["HF_TOKEN"], status_age_s=1200)
    killed = decide(stale_block, rules, 0)
    assert killed.action == "kill" and killed.rule == "blocked_for=15m"
    assert "HF_TOKEN" in killed.detail
    display(killed.model_dump())


# %%
if test():
    for field, rule in (("status_age_s", "status_stale_for=30m"),
                        ("notebook_age_s", "no_progress_for=45m"),
                        ("gpu_idle_s", "gpu_idle_for=20m")):
        decision = decide(make(**{field: 10_000}), rules, 0)
        assert decision.action == "kill", field
        assert decision.rule == rule, (field, decision.rule)

    # Ordering matters: a finished run that is ALSO stale is done, never "killed for
    # staleness". Reporting a success as a kill is the one wrong answer here.
    both = make(state="finished", announced=True, status_age_s=10_000,
                notebook_age_s=10_000)
    assert decide(both, rules, 0).action == "done"
    display(decide(both, rules, 0).model_dump())


# %%
if test():
    ending = make(time_left_s=120)
    assert decide(ending, rules, 0).action == "renew"
    assert decide(make(time_left_s=120, leases_used=4, max_leases=4), rules, 0).action == "escalate"

    # A batch job's walltime is a real deadline, so it is watched, never renewed.
    assert decide(make(mode="batch", time_left_s=120), rules, 0).action == "watch"

    healthy = decide(make(round="2/3"), rules, 0)
    assert healthy.action == "watch" and "2/3" in healthy.detail
    display(healthy.model_dump())


# %% [markdown]
# ## Turning a probe into views

# %%
def views(snapshot: dict, cluster: ClusterConfig, now: float | None = None) -> list[AgentView]:
    """Merge one probe blob into one row per agent. Pure: testable on a fixture."""
    now = snapshot.get("now", now or time.time())
    jobs = {j.job_id: j for j in _parse_queue(snapshot.get("queue", ""))}
    rows = []
    for entry in snapshot.get("runs", []):
        launch = entry.get("launch") or {}
        status = entry.get("status") or {}
        job = jobs.get(str(launch.get("job_id", "")))
        state = status.get("state", "unknown")
        if state not in {"running", "needs_env", "needs_human", "finished", "failed"}:
            state = "unknown"
        rows.append(AgentView(
            session_id=launch.get("session_id", entry.get("run_dir", "?").rsplit("/", 1)[-1]),
            task=launch.get("task", "?"),
            mode=launch.get("mode", "interactive"),
            job_id=str(launch.get("job_id", "")),
            job_state=_job_state(job),
            time_left_s=job.time_left_s if job else None,
            state=state,
            round=status.get("round"),
            waiting_on=status.get("waiting_on") or [],
            cells_done=status.get("cells_done"),
            status_age_s=_age(now, entry.get("status_mtime")),
            notebook_age_s=_age(now, entry.get("nb_mtime")),
            gpu_usd=job.gpu_usd(cluster) if job else 0.0,
            leases_used=launch.get("leases_used", 1),
            max_leases=launch.get("max_leases", 4),
            announced=bool(status.get("announced_on")),
            run_dir=entry.get("run_dir", ""),
        ))
    return rows


def _age(now: float, mtime: object) -> int | None:
    """Seconds since a file changed. A file that has never existed has no age."""
    return None if not mtime else max(0, int(now - int(mtime)))


def _job_state(job: Job | None) -> str:
    if job is None:
        return "GONE"
    return {"R": "RUNNING", "PD": "PENDING", "CF": "PENDING",
            "TO": "TIMEOUT", "F": "FAILED"}.get(job.state, "GONE")


def _parse_queue(raw: str) -> list[Job]:
    from slurm_agent.jobs import _parse_gpus, _parse_time

    jobs = []
    for line in raw.splitlines():
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 7 or not fields[1]:
            continue
        jobs.append(Job(name=fields[0], job_id=fields[1], node=fields[2] or None,
                        state=fields[3], time_left_s=_parse_time(fields[4]),
                        elapsed_s=_parse_time(fields[5]) or 0,
                        gpus=_parse_gpus(fields[6]), batch=fields[0].startswith("sa-")))
    return jobs


# %%
if test():
    import json as _json
    from pathlib import Path as _Path

    fixture = _Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "probe.json"
    snapshot = _json.loads(fixture.read_text())
    cluster = ClusterConfig(login_host="h")
    rows = {v.session_id: v for v in views(snapshot, cluster)}

    assert set(rows) == {"4f2c", "7b19", "9c03"}
    live = rows["4f2c"]
    assert live.job_state == "RUNNING" and live.round == "2/3"
    # Both ages present and INDEPENDENT: the agent's self-report and the observed file.
    assert live.status_age_s == 120 and live.notebook_age_s == 600

    # A notebook that has never been written has no age, so no_progress_for cannot fire —
    # an absent file is not evidence of a stuck agent.
    assert rows["7b19"].notebook_age_s is None
    assert decide(rows["7b19"], SupervisionConfig(), 0).action == "kill"

    # A job squeue no longer knows reads as GONE, not as still running.
    assert rows["9c03"].job_state == "GONE"
    assert decide(rows["9c03"], SupervisionConfig(), 0).action == "done"
    display({k: v.model_dump(include={"job_state", "state", "status_age_s",
                                      "notebook_age_s"}) for k, v in rows.items()})


# %%
if test():
    # An unreadable or absent status block degrades to `unknown` with an age from the
    # file, never crashing the loop — supervision must survive a half-written file.
    odd = {"now": 100, "queue": "", "runs": [
        {"run_dir": "/r/x", "nb_mtime": 0, "nb_bytes": 0, "status_mtime": 40,
         "launch": {"session_id": "x", "task": "T", "job_id": "1"}, "status": None}]}
    view = views(odd, ClusterConfig(login_host="h"))[0]
    assert view.state == "unknown" and view.status_age_s == 60
    display(view.model_dump(include={"state", "status_age_s", "job_state"}))


# %% [markdown]
# ## Acting, and the loop
#
# The loop holds no state. It is safe to kill and restart — a closed laptop loses nothing,
# because every fact is re-derived from the next probe.

# %%
def act(decision: Decision, view: AgentView, run: Runner, cluster: ClusterConfig,
        rules: SupervisionConfig, *, auto_renew: bool = False,
        notified: set[tuple[str, str]] | None = None) -> str:
    """Carry out a decision and return the line that goes in the session notebook."""
    notified = notified if notified is not None else set()
    tag = f"{view.session_id} {view.task}"

    if decision.action == "kill":
        kill_step(view, run, reason=decision.rule or "rule")
        saved = view.gpu_usd
        return (f"{tag} KILL [{decision.rule}] {decision.detail} · "
                f"~${saved:.2f}/h freed · staged state kept · "
                f"resume: poe agent-continue {view.session_id}")

    if decision.action == "escalate":
        key = (view.session_id, decision.detail)
        if key in notified:
            return f"{tag} escalate (already sent) {decision.detail}"
        notified.add(key)
        _escalate(view, decision, cluster)
        return f"{tag} ESCALATE {decision.detail}"

    if decision.action == "renew":
        if not auto_renew:
            # Rules kill; the MANAGER renews. Renewal needs somebody to read the log and
            # the notebook, which a threshold cannot do — so it is proposed, not taken.
            return (f"{tag} renew? lease {view.leases_used}/{view.max_leases} ending — "
                    f"read the notebook, then: poe agent-continue {view.session_id}")
        return f"{tag} renew requested (auto) — poe agent-continue {view.session_id}"

    if decision.action == "done":
        return f"{tag} done · {decision.detail}"
    return f"{tag} ok · {decision.detail}"


def _escalate(view: AgentView, decision: Decision, cluster: ClusterConfig) -> None:
    """Tell the human. Never lets a notification failure take the loop down with it."""
    try:
        from slurm_agent.config import ManagerConfig, declared_env_keys, load
        from slurm_agent.notify import NotifyConfig, notify

        cfg = load("config/notify.yaml", NotifyConfig)
        keys = declared_env_keys(load("config/manager.yaml", ManagerConfig), [])
        notify(f"[slurm-agent] {view.task} needs you", 
               f"{decision.detail}\nsession {view.session_id}\nrun dir {view.run_dir}\n",
               cfg, keys)
    except Exception as exc:  # noqa: BLE001 - a broken channel must not stop supervision
        log.error("watch.escalate_failed", session=view.session_id, error=str(exc))


def kill_step(view: AgentView, run: Runner, *, reason: str) -> str:
    """Stop ONE agent.

    Interactive allocations are shared, so this cancels the agent's job STEP and leaves the
    allocation up for its neighbours. Only a batch job — which is this agent's alone — is
    cancelled whole.
    """
    target = view.job_id if view.mode == "batch" else _step_of(view, run)
    if target:
        run(f"scancel {quote(target)}")
    log.info("watch.killed", session=view.session_id, target=target, rule=reason)
    return target or ""


def _step_of(view: AgentView, run: Runner) -> str | None:
    """The job step this agent occupies, so a kill does not take its neighbours down."""
    out = run(f"squeue --job={quote(view.job_id)} --steps --noheader --Format=StepID:|,Name:|")
    for line in out.splitlines():
        fields = [f.strip() for f in line.split("|")]
        if len(fields) >= 2 and view.session_id[:8] in fields[1]:
            return fields[0]
    return None


def watch(run: Runner, cluster: ClusterConfig, rules: SupervisionConfig, *,
          once: bool = False, auto_renew: bool = False, sleep=time.sleep) -> list[str]:
    """poll → decide → act → log, forever. Holds no state, so it is safe to restart."""
    notified: set[tuple[str, str]] = set()
    lines: list[str] = []
    while True:
        try:
            snapshot = probe(run, cluster.run_root)
        except RemoteError as exc:
            # One skipped cycle. The next probe re-derives everything, so a dropped
            # connection is never a reason to lose supervision.
            log.warning("watch.probe_failed", error=str(exc))
            if once:
                return lines
            sleep(duration_seconds(rules.poll_every))
            continue
        for view in views(snapshot, cluster):
            line = act(decide(view, rules, snapshot.get("now", time.time())), view, run,
                       cluster, rules, auto_renew=auto_renew, notified=notified)
            lines.append(line)
            print(line)
        if once:
            return lines
        sleep(duration_seconds(rules.poll_every))


# %%
if test():
    from tests.conftest import FakeRunner

    shared = AgentView(session_id="4f2c1111", task="T", job_id="62526",
                       job_state="RUNNING", state="running", mode="interactive",
                       status_age_s=10_000, gpu_usd=2.9)
    runner = FakeRunner({"--steps": "62526.2|claude-4f2c1111|\n"})
    line = act(decide(shared, rules, 0), shared, runner, ClusterConfig(login_host="h"), rules)
    assert "KILL" in line and "status_stale_for" in line
    # The STEP is cancelled, never the shared allocation its neighbours are on.
    assert runner.asked("scancel 62526.2")
    assert not runner.asked("scancel 62526 ")
    display(line)


# %%
if test():
    batch = shared.model_copy(update={"mode": "batch", "session_id": "7b19"})
    batch_runner = FakeRunner()
    act(decide(batch, rules, 0), batch, batch_runner, ClusterConfig(login_host="h"), rules)
    assert batch_runner.asked("scancel 62526")     # a batch job is this agent's alone


# %%
if test():
    # renew is PROPOSED, not taken: nothing is submitted without --auto-renew.
    ending = AgentView(session_id="4f2c", task="T", job_id="1", job_state="RUNNING",
                       state="running", time_left_s=60, leases_used=1, max_leases=4)
    quiet = FakeRunner()
    line = act(decide(ending, rules, 0), ending, quiet, ClusterConfig(login_host="h"), rules)
    assert "renew?" in line and "agent-continue" in line
    assert quiet.commands == []
    display(line)


# %%
if test():
    # A failing probe skips the cycle and the loop survives.
    class _Flaky(FakeRunner):
        def __call__(self, command, stdin=None):
            self.commands.append(command)
            raise RemoteError("probe", "connection reset")

    assert watch(_Flaky(), ClusterConfig(login_host="h"), rules, once=True) == []
