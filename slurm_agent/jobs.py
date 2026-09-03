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
# # Allocations
#
# Bringing up an allocation, finding the one already running, tearing it down, and getting
# a shell on its node.
#
# Two facts shape everything here:
#
# * **`salloc --no-shell`** means the allocation does not die with the ssh connection that
#   asked for it — a closed laptop must not take an agent's job with it.
# * **Tillicum permits one interactive allocation.** The cap is on *allocations*, not
#   agents: agents attach as `srun --overlap` job steps, so many share one. `job_up` never
#   submits a second, because it would queue behind the user's own job.

# %%
import re
import time
from pathlib import Path

import structlog
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict

from slurm_agent.config import ClusterConfig, duration_seconds
from slurm_agent.remote import Runner, quote

log = structlog.get_logger(__name__)

# One squeue call, one explicit format, so parsing never depends on column defaults.
SQUEUE_FORMAT = "Name:|,JobID:|,NodeList:|,StateCompact:|,TimeLeft:|,TimeUsed:|,tres-alloc:|"


# %%
class Job(BaseModel):
    """One allocation, as squeue sees it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    job_id: str
    node: str | None
    state: str
    time_left_s: int | None
    elapsed_s: int
    gpus: int
    batch: bool = False

    def gpu_usd(self, cluster: ClusterConfig) -> float:
        """What this allocation has cost so far in GPU time."""
        return self.gpus * (self.elapsed_s / 3600) * cluster.gpu_usd_per_hour


# %%
def _parse_gpus(tres: str) -> int:
    """Read the GPU count out of a SLURM tres string like `cpu=8,mem=200G,gres/gpu=2`."""
    match = re.search(r"gres/gpu(?::\w+)?=(\d+)", tres)
    return int(match.group(1)) if match else 0


def _parse_time(text: str) -> int | None:
    """SLURM prints `UNLIMITED`, `N/A`, `1-04:00:00` or `HH:MM:SS`."""
    text = text.strip()
    if not text or text in {"UNLIMITED", "N/A", "INVALID"}:
        return None
    days, _, clock = text.partition("-")
    if clock:
        return int(days) * 86400 + duration_seconds(clock)
    return duration_seconds(text)


# %%
if test():
    assert _parse_gpus("cpu=8,mem=200G,gres/gpu=2") == 2
    assert _parse_gpus("cpu=8,mem=200G,gres/gpu:h200=4") == 4
    assert _parse_gpus("cpu=8,mem=200G") == 0
    assert _parse_time("03:58:00") == 14280
    assert _parse_time("1-04:00:00") == 100800
    assert _parse_time("UNLIMITED") is None
    display({"gpus": _parse_gpus("cpu=8,gres/gpu=2"), "left": _parse_time("1-04:00:00")})


# %% [markdown]
# ## Reading the queue

# %%
def job_list(run: Runner) -> list[Job]:
    """Every allocation of mine. One squeue call."""
    output = run(f"squeue --me --noheader --Format={quote(SQUEUE_FORMAT)}")
    jobs = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 7:
            continue
        name, job_id, node, state, left, used, tres = fields[:7]
        jobs.append(Job(
            name=name, job_id=job_id, node=node or None, state=state,
            time_left_s=_parse_time(left), elapsed_s=_parse_time(used) or 0,
            gpus=_parse_gpus(tres), batch=name.startswith("sa-"),
        ))
    return jobs


def find_job(jobs: list[Job], name: str) -> Job | None:
    """The live allocation under `name`, if there is one."""
    return next((j for j in jobs if j.name == name and j.state in {"R", "PD", "CF"}), None)


def interactive_jobs(jobs: list[Job]) -> list[Job]:
    """My live interactive allocations. Tillicum permits one."""
    return [j for j in jobs if not j.batch and j.state in {"R", "PD", "CF"}]


# %%
if test():
    from tests.conftest import FakeRunner

    rows = (
        "remote_dev|62526|g004|R|03:58:00|00:02:00|cpu=8,mem=200G,gres/gpu=2|\n"
        "sa-ablation|62531|g011|R|00:12:00|07:48:00|cpu=8,mem=200G,gres/gpu=4|\n"
    )
    jobs = job_list(FakeRunner({"squeue": rows}))
    assert [j.job_id for j in jobs] == ["62526", "62531"]
    assert jobs[0].gpus == 2 and jobs[0].node == "g004"
    assert jobs[1].batch is True                       # sa- prefix marks ours as batch
    assert len(interactive_jobs(jobs)) == 1
    assert find_job(jobs, "remote_dev").job_id == "62526"
    assert find_job(jobs, "absent") is None
    assert round(jobs[1].gpu_usd(ClusterConfig(login_host="h")), 2) == 28.08
    display([j.model_dump() for j in jobs])


# %%
if test():
    # An empty queue is an ordinary state, not an error.
    assert job_list(FakeRunner({"squeue": "\n"})) == []


# %% [markdown]
# ## Bringing one up

# %%
def job_up(name: str, run: Runner, cluster: ClusterConfig, *, gpus: int = 1,
           time_limit: str = "04:00:00", qos: str | None = None, cpus: int = 8,
           mem: str = "200G", wait_s: int = 300, poll_s: int = 5) -> Job:
    """Bring up an allocation named `name`, or return the live one.

    Never submits a second interactive allocation: Tillicum permits one, so a second
    request would simply queue behind the first. Agents share an allocation as job steps.
    """
    existing = find_job(job_list(run), name)
    if existing:
        log.info("job.reattach", name=name, job_id=existing.job_id)
        return _settle(existing, run, cluster, name, wait_s, poll_s)

    others = interactive_jobs(job_list(run))
    if others:
        other = others[0]
        log.warning("job.interactive_cap", requested=name, existing=other.name,
                    job_id=other.job_id)
        return other

    flags = [f"--job-name={name}", f"--gpus={gpus}", f"--cpus-per-task={cpus}",
             f"--mem={mem}", f"--time={time_limit}",
             f"--qos={qos or cluster.default_qos}"]
    if cluster.account:
        flags.append(f"--account={cluster.account}")
    salloc = "salloc --no-shell " + " ".join(flags)
    if cluster.allocation_mode == "tmux":
        # Fallback for a site that refuses --no-shell: a detached tmux session on the
        # login node holds an ordinary salloc, which survives the ssh dropping.
        salloc = f"tmux new-session -d -s {quote(name)} {quote('salloc ' + ' '.join(flags))}"
    run(salloc)
    return _settle(None, run, cluster, name, wait_s, poll_s)


def _settle(job: Job | None, run: Runner, cluster: ClusterConfig, name: str,
            wait_s: int, poll_s: int) -> Job:
    """Wait for the allocation to start, then point ssh/vscode at its node."""
    deadline = time.monotonic() + wait_s
    while True:
        job = find_job(job_list(run), name) if job is None or job.state != "R" else job
        if job and job.state == "R" and job.node:
            update_node_config(job.node, cluster.node_config_path)
            return job
        if time.monotonic() >= deadline:
            if job:
                # A queued job is not a failure — return it and let the caller decide.
                log.warning("job.still_pending", name=name, job_id=job.job_id)
                return job
            raise LookupError(f"allocation {name!r} never appeared in squeue")
        time.sleep(poll_s)
        job = None


# %% [markdown]
# ## Tearing down, and getting a shell

# %%
def job_down(name: str, run: Runner) -> str:
    """Cancel the allocation named `name`. Returns the cancelled job id."""
    jobs = job_list(run)
    job = find_job(jobs, name)
    if not job:
        live = ", ".join(j.name for j in jobs) or "none"
        raise LookupError(f"no live allocation named {name!r} — mine are: {live}")
    run(f"scancel {quote(job.job_id)}")
    log.info("job.cancelled", name=name, job_id=job.job_id)
    return job.job_id


def job_shell_command(name: str, run: Runner, cluster: ClusterConfig) -> list[str]:
    """The argv for an interactive shell ON the compute node.

    Returned rather than run, so the CLI can `os.execvp` it and genuinely become the shell.
    """
    job = find_job(job_list(run), name)
    if not job or job.state != "R":
        raise LookupError(f"no running allocation named {name!r} — try `poe job-up {name}`")
    inner = f"srun --jobid={job.job_id} --overlap --pty bash -l"
    return ["ssh", "-t", cluster.login_host, inner]


def update_node_config(node: str, config_path: Path | str) -> str:
    """Rewrite the `Hostname` line in the ssh node config so vscode follows the node."""
    path = Path(str(config_path)).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `poe init` to install the ssh config templates"
        )
    updated = re.sub(r"(?im)^(\s*hostname)\s+\S+", rf"\1 {node}", path.read_text())
    path.write_text(updated)
    log.info("job.node_config", path=str(path), node=node)
    return node


# %%
if test():
    runner = FakeRunner({"squeue": "remote_dev|62526|g004|R|03:58:00|00:02:00|gres/gpu=2|\n"})
    assert job_down("remote_dev", runner) == "62526"
    assert runner.asked("scancel 62526")

    try:
        job_down("absent", runner)
        raise AssertionError("unknown name should have raised")
    except LookupError as exc:
        assert "remote_dev" in str(exc)          # the message lists what IS live
        display(str(exc))


# %%
if test():
    cluster_cfg = ClusterConfig(login_host="tillicum-login")
    argv = job_shell_command("remote_dev", runner, cluster_cfg)
    assert argv[:3] == ["ssh", "-t", "tillicum-login"]
    assert "--jobid=62526" in argv[3] and "--overlap" in argv[3]
    display(argv)


# %%
if test():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "tillicum-node-config"
        cfg.write_text("Host tillicum-node\n  Hostname g001\n  User deanlcs\n")
        update_node_config("g004", cfg)
        text = cfg.read_text()
        assert "Hostname g004" in text
        assert "User deanlcs" in text            # untouched lines survive
        display(text)

        try:
            update_node_config("g004", Path(tmp) / "absent")
            raise AssertionError("missing config should have raised")
        except FileNotFoundError as exc:
            assert "poe init" in str(exc)


# %%
if test():
    # job_up reattaches rather than submitting, and never opens a second interactive job.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "node-config"
        cfg.write_text("Hostname g000\n")
        cluster_cfg = ClusterConfig(login_host="h", node_config_path=cfg)

        reattach = FakeRunner({"squeue": "remote_dev|62526|g004|R|03:58:00|00:02:00|gres/gpu=2|\n"})
        job = job_up("remote_dev", reattach, cluster_cfg)
        assert job.job_id == "62526"
        assert not reattach.asked("salloc")       # the reattach path submits nothing
        assert "Hostname g004" in cfg.read_text()

        # A DIFFERENT interactive allocation is returned with a warning, not queued behind.
        busy = FakeRunner({"squeue": "other|62000|g002|R|01:00:00|00:30:00|gres/gpu=1|\n"})
        assert job_up("remote_dev", busy, cluster_cfg).name == "other"
        assert not busy.asked("salloc")
        display("interactive cap respected: returned 'other' instead of submitting")
