# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Launching a remote agent
#
# We build an **argv**, not a runtime. The Claude Code CLI already does almost everything
# this layer needs — `--session-id` gives us the handle, `--max-budget-usd` the cap,
# `--allowed-tools` and `--mcp-config --strict-mcp-config` the sandbox, `--add-dir` the
# filesystem scope, `--settings` the hooks, `--resume` the continuation.
#
# `claude_argv` is pure, and it is the audit surface: reading it tells you exactly what an
# agent may do. It never emits `--bare`, for three independent reasons — `--bare` restricts
# auth to `ANTHROPIC_API_KEY` and never reads the OAuth subscription Tillicum is logged in
# with, it skips hooks (which is where liveness comes from), and it skips `CLAUDE.md`
# discovery (which is how the staged repo states its own conventions).

# %%
import json
import uuid
from importlib.resources import files
from pathlib import Path

import structlog
from IPython.display import display
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from juplit import test

from slurm_agent.config import AgentConfig, ClusterConfig
from slurm_agent.jobs import find_job, job_list
from slurm_agent.remote import Runner, quote, remote_path
from slurm_agent.staging import MissingEnvError, missing_env_remote, stage

log = structlog.get_logger(__name__)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class ContentionError(RuntimeError):
    """The shared allocation has no spare GPUs for another agent."""


class LeaseExhausted(RuntimeError):
    """This run has used every lease its config allows; a human decides what happens next."""


# %%
def claude_argv(agent: AgentConfig, *, prompt: str, session_id: str, settings_path: str,
                mcp_config_path: str | None = None, resume: bool = False) -> list[str]:
    """Build the exact `claude` command line. Pure — this is the audit of what it may do."""
    argv = ["claude", "-p", prompt]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    argv += [
        "--output-format", "json",
        # No interactive prompt can ever block a run nobody is watching.
        "--permission-mode", "dontAsk",
        "--max-budget-usd", str(agent.max_budget_usd),
        "--add-dir", agent.workdir,
        "--settings", settings_path,
    ]
    if agent.allowed_tools:
        argv += ["--allowed-tools", *agent.allowed_tools]
    if agent.mcp and mcp_config_path:
        # Both together, always: --mcp-config without --strict would leave the agent's
        # ambient MCP servers reachable, which the declared config is meant to bound.
        argv += ["--mcp-config", mcp_config_path, "--strict-mcp-config"]
    if agent.model:
        argv += ["--model", agent.model]
    return argv


# %%
if test():
    agent = AgentConfig(repo="DeanLight/baselines", ref="claude/exp14",
                        workdir="~/work/baselines", log_dir="experiments/{EXP_ID}",
                        max_budget_usd=8, mcp=["notion"], requires_env=["HF_TOKEN"],
                        allowed_tools=["Read", "Bash(uv run *)"])
    argv = claude_argv(agent, prompt="go", session_id="4f2c", settings_path="/run/settings.json",
                       mcp_config_path="/run/mcp.json")

    assert argv[:3] == ["claude", "-p", "go"]
    assert "--session-id" in argv and "4f2c" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-budget-usd") + 1] == "8.0"
    assert "Bash(uv run *)" in argv
    assert "--strict-mcp-config" in argv
    display(argv)


# %%
if test():
    # The regression guard: --bare would break auth, hooks AND CLAUDE.md discovery.
    assert "--bare" not in argv

    # No argv element points inside the staged repo except the deliberate --add-dir scope.
    inside = [a for a in argv if "work/baselines" in a]
    assert inside == ["~/work/baselines"]

    # mcp: [] omits BOTH mcp flags rather than one of them.
    plain = AgentConfig(repo="r", ref="v", workdir="~/w", log_dir="d", max_budget_usd=1)
    bare_argv = claude_argv(plain, prompt="go", session_id="s", settings_path="/s.json")
    assert "--mcp-config" not in bare_argv and "--strict-mcp-config" not in bare_argv

    resumed = claude_argv(agent, prompt="continue", session_id="4f2c",
                          settings_path="/run/settings.json", resume=True)
    assert "--resume" in resumed and "--session-id" not in resumed
    assert resumed[resumed.index("--max-budget-usd") + 1] == "8.0"   # flags survive a resume
    display(resumed[:6])


# %% [markdown]
# ## Laying out the run root
#
# **Nothing this repo writes ever lands inside the staged repo.** `stage()` refuses a dirty
# tree, and that refusal is what stops an experiment silently measuring unreviewed code —
# so our own files must not be what dirties it. Everything goes in the run root, addressed
# absolutely through `SLURM_AGENT_RUN_DIR`.

# %%
def render(template: str, **context: object) -> str:
    """Render a repo template. StrictUndefined, so a missing variable fails loudly."""
    env = Environment(loader=FileSystemLoader(PROMPTS), undefined=StrictUndefined,
                      keep_trailing_newline=True)
    return env.get_template(template).render(**context)


def launch_prompt(agent: AgentConfig, *, task: str, log_dir: str, run_dir: str,
                  sha: str) -> str:
    """The launch prompt: the task, the log directory, and the mailbox contract.

    Which brief an agent gets is its own declared property, so the same launcher can put a
    twelve-hour experiment agent and a two-minute smoke agent on the same allocation.
    Every brief takes the same variables — that is what makes them interchangeable.
    """
    return render(agent.prompt, task=task, repo=agent.repo, ref=agent.ref,
                  sha=sha[:8], workdir=agent.workdir, log_dir=log_dir,
                  run_dir=run_dir, lease=agent.lease)


def _write_remote(run: Runner, path: str, content: str) -> None:
    """Write a file on the cluster via a quoted heredoc — no scp round trip."""
    run(f"cat > {path} <<'SLURM_AGENT_EOF'\n{content}\nSLURM_AGENT_EOF")


def resolve_log_dir(agent: AgentConfig, run_dir: str, exp_id: str) -> str:
    """Where this run's notebooks go, absolute.

    `{EXP_ID}` keeps two runs of one agent out of each other's notebook. `{RUN_DIR}` is the
    other case, and it is what lets an agent produce a deliverable without touching the
    repo at all: a log dir under the run root leaves the staged tree clean, so the next
    launch is not refused by work the last one did. An agent whose output belongs to the
    repo — an experiment write-up — uses a path relative to the workdir, as before.
    """
    log_dir = agent.log_dir.replace("{EXP_ID}", exp_id)
    if "{RUN_DIR}" in log_dir:
        return log_dir.replace("{RUN_DIR}", run_dir.rstrip("/"))
    return f"{agent.workdir.rstrip('/')}/{log_dir}"


def prepare_run(agent: AgentConfig, task: str, run: Runner, cluster: ClusterConfig,
                exp_id: str | None = None) -> tuple[str, str, str]:
    """Stage, preflight, and lay out the run root. Returns (session_id, run_dir, sha)."""
    sha = stage(agent, run)
    missing = missing_env_remote(agent, run)
    if missing:
        raise MissingEnvError(missing, f"{agent.workdir}/.envrc")

    session_id = str(uuid.uuid4())
    run_dir = f"{cluster.run_root.rstrip('/')}/{session_id}"
    quoted_dir = remote_path(run_dir)
    run(f"mkdir -p {quoted_dir}")

    abs_log_dir = resolve_log_dir(agent, run_dir, exp_id or task.lower())

    assets = files("slurm_agent") / "assets"
    _write_remote(run, f"{quoted_dir}/remote_status.py",
                  (assets / "remote_status.py").read_text())
    _write_remote(run, f"{quoted_dir}/settings.json", render_asset(
        "hook_settings.json.jinja", run_dir=run_dir, log_dir=abs_log_dir))
    _write_remote(run, f"{quoted_dir}/launch.json", json.dumps({
        "session_id": session_id, "task": task, "agent": agent.repo, "mode": agent.mode,
        "repo": agent.repo, "ref": agent.ref, "sha": sha, "workdir": agent.workdir,
        "log_dir": abs_log_dir, "lease": agent.lease, "max_leases": agent.max_leases,
        "leases_used": 1, "max_budget_usd": agent.max_budget_usd, "run_dir": run_dir,
    }, indent=1, sort_keys=True))
    log.info("launch.prepared", session=session_id, run_dir=run_dir)
    return session_id, run_dir, sha


def render_asset(name: str, **context: object) -> str:
    """Render a template that ships inside the package rather than in `prompts/`."""
    text = (files("slurm_agent") / "assets" / name).read_text()
    return Environment(undefined=StrictUndefined).from_string(text).render(**context)


# %%
if test():
    # An agent that claims no GPU is never refused for capacity — that is what makes "two
    # small tasks, one allocation" the default rather than a thing you have to argue for.
    free = AgentConfig(repo="r", ref="main", workdir="~/work/x", log_dir="{RUN_DIR}/e",
                       max_budget_usd=1, gpus=0)
    assert free.gpus == 0

    smoke_cfg = AgentConfig(repo="r", ref="main", workdir="~/work/x", max_budget_usd=1,
                            log_dir="{RUN_DIR}/evidence")
    # A run-root log dir is absolute and never joined to the workdir — that is what lets an
    # agent produce a deliverable while leaving the staged tree clean.
    assert resolve_log_dir(smoke_cfg, "/home/d/.slurm-agent/runs/4f2c", "smoke") == \
        "/home/d/.slurm-agent/runs/4f2c/evidence"

    exp_cfg = AgentConfig(repo="r", ref="main", workdir="~/work/x", max_budget_usd=1,
                          log_dir="experiments/{EXP_ID}")
    assert resolve_log_dir(exp_cfg, "/run/4f2c", "exp14") == "~/work/x/experiments/exp14"


# %%
if test():
    from tests.conftest import FakeRunner

    prompt = launch_prompt(agent, task="TASK-104", log_dir="experiments/exp14",
                           run_dir="/home/d/.slurm-agent/runs/4f2c", sha="a1b2c3d4e5")
    assert "TASK-104" in prompt
    assert "experiments/exp14" in prompt
    assert "remote_status.py" in prompt
    assert "Dev Workspace" in prompt
    # It must tell the agent to look before it writes: continue an existing notebook, or
    # start a new one. An analysis grows several, and picking wrong silently forks it.
    assert "Look in it before you start" in prompt
    assert "continue it" in prompt
    # It must tell the agent NOT to cancel a shared allocation.
    assert "Do not cancel the allocation" in prompt
    display(prompt[:400])


# %%
if test():
    settings = json.loads(render_asset("hook_settings.json.jinja",
                                       run_dir="/run/4f2c", log_dir="/w/experiments/exp14"))
    assert set(settings["hooks"]) == {"PostToolUse", "Stop", "SessionEnd"}
    stop = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "/run/4f2c/remote_status.py tick" in stop
    # The hooks are pointed at the log DIRECTORY; which notebook is current is discovered,
    # never baked in at launch, because the agent may start another one.
    assert "--log-dir /w/experiments/exp14" in stop
    assert "/w/nb.ipynb" not in json.dumps(settings)

    # The reminder is a hook, not a line in the prompt, so it lands when it matters.
    remind = settings["hooks"]["PostToolUse"][0]
    assert remind["matcher"]["tools"] == ["Bash", "Edit", "Write", "NotebookEdit"]
    assert "remote_status.py remind" in remind["hooks"][0]["command"]
    display(settings)


# %% [markdown]
# ## Firing it
#
# Detached, so closing the laptop cannot kill it: `setsid nohup` puts the `srun` client in
# its own session on the login node, where it survives the ssh connection dropping.

# %%
def _detached(job_id: str, agent: AgentConfig, run_dir: str, argv: list[str]) -> str:
    """The one-liner that starts an agent as a job step and returns immediately."""
    # `quote` alone would quote the tilde in a home-relative argument — `--settings`,
    # `--mcp-config` and `--add-dir` all carry one — and the remote shell would receive it
    # literally. `claude` then dies at launch with "Settings file not found: ~/...", having
    # announced nothing, which reads like an agent that never started rather than a path
    # bug. The body runs under `bash -lc`, so `"$HOME"` expands there exactly as it already
    # does for the `cd` below.
    inner = " ".join(remote_path(a) if a.startswith("~/") else quote(a) for a in argv)
    workdir = remote_path(agent.workdir)
    quoted_dir = remote_path(run_dir)
    body = (f"cd {workdir} && "
            f"[ -f .envrc ] && . ./.envrc; "
            f"export SLURM_AGENT_RUN_DIR={quoted_dir}; exec {inner}")
    return (f"setsid nohup srun --jobid={quote(job_id)} --overlap "
            f"--chdir={workdir} "
            f"--output={quoted_dir}/agent.log --error={quoted_dir}/agent.err "
            f"bash -lc {quote(body)} </dev/null >/dev/null 2>&1 &")


def launch(agent: AgentConfig, task: str, job_name: str, run: Runner,
           cluster: ClusterConfig, *, exp_id: str | None = None,
           gpus_needed: int | None = None) -> str:
    """Stage, preflight and start a Claude agent on an allocation. Returns the session id.

    How many GPUs an agent claims is its own declared property. An agent that claims none
    is never refused, which is the whole point: several small tasks belong as steps on one
    allocation, not as several allocations on a cluster that permits one interactive job.
    """
    gpus_needed = agent.gpus if gpus_needed is None else gpus_needed
    job = find_job(job_list(run), job_name)
    if not job or job.state != "R":
        raise LookupError(f"no running allocation named {job_name!r} — try `poe job-up`")

    live = _agents_on(job.job_id, run)
    # Conservative on purpose: every live agent is assumed to claim as much as this one.
    # Over-counting costs a refusal you can read and act on; under-counting costs two
    # agents fighting over one device, hours later, looking like a cluster fault.
    if gpus_needed and job.gpus - len(live) * gpus_needed < gpus_needed:
        raise ContentionError(
            f"allocation {job_name!r} has {job.gpus} gpu and {len(live)} agent(s) on it, "
            f"and {agent.repo} claims {gpus_needed}. Size it larger with `poe job-up "
            "--gpus`, declare `gpus: 0` if it needs none, or use `poe agent-batch`."
        )

    session_id, run_dir, sha = prepare_run(agent, task, run, cluster, exp_id)
    log_dir = resolve_log_dir(agent, run_dir, exp_id or task.lower())
    prompt = launch_prompt(agent, task=task, log_dir=log_dir, run_dir=run_dir, sha=sha)
    argv = claude_argv(
        agent, prompt=prompt, session_id=session_id,
        settings_path=f"{run_dir}/settings.json",
        mcp_config_path=f"{run_dir}/mcp.json" if agent.mcp else None,
    )
    if agent.mcp:
        _write_remote(run, f"{remote_path(run_dir)}/mcp.json",
                      (Path("config/mcp.json").read_text()))
    run(_detached(job.job_id, agent, run_dir, argv))
    log.info("launch.started", session=session_id, job=job.job_id, task=task)
    return session_id


# `%i` rather than `--Format=StepID:|`. The `|` there was meant as a literal field suffix
# and reached the REMOTE shell unquoted, where it is a pipe: every launch onto a live
# allocation died on `syntax error: unexpected end of file`, which reads like a cluster
# fault and is not one. Anything with shell meaning must be quoted before it crosses ssh —
# the same rule as `$HOME` vs `~`, in the other direction.
STEP_FORMAT = "%i"


def _agents_on(job_id: str, run: Runner) -> list[str]:
    """Step ids already running on this allocation — how many agents are sharing it."""
    out = run(f"squeue --job={quote(job_id)} --steps --noheader "
              f"--format={quote(STEP_FORMAT)}")
    return [line.strip() for line in out.splitlines() if line.strip()]


# %%
if test():
    # The bug this cost: a literal `|` in a remote command is a PIPE on the far side, so
    # every launch onto a live allocation died with `syntax error: unexpected end of file`.
    from tests.conftest import FakeRunner as _Fake

    steps = _Fake({"squeue --job": "295750.0\n295750.1\n"})
    assert _agents_on("295750", steps) == ["295750.0", "295750.1"]
    asked = steps.commands[0]
    # The real test is what the REMOTE shell would make of it: splitting it must give back
    # exactly the words we meant, with no operator among them. `%` has no shell meaning, so
    # `shlex.quote` rightly leaves it bare; `|` does, and that is what bit.
    import shlex

    assert shlex.split(asked) == ["squeue", "--job=295750", "--steps", "--noheader",
                                  "--format=%i"], asked
    assert not any(ch in asked for ch in "|;&<>"), f"a shell operator crosses ssh: {asked}"


def continue_run(session_id: str, job_name: str, run: Runner, cluster: ClusterConfig,
                 agent: AgentConfig) -> str:
    """A fresh lease on the same notebook: same session, same workdir, `--resume`.

    Cheap because run cells are idempotent — finished rounds skip on their DONE markers and
    the run picks up where the kill stopped. Nothing is re-staged.
    """
    run_dir = f"{cluster.run_root.rstrip('/')}/{session_id}"
    record = json.loads(run(f"cat {remote_path(run_dir)}/launch.json"))
    if record["leases_used"] >= record["max_leases"]:
        raise LeaseExhausted(
            f"{session_id} has used {record['leases_used']}/{record['max_leases']} leases — "
            "a human decides whether this run continues"
        )
    job = find_job(job_list(run), job_name)
    if not job or job.state != "R":
        raise LookupError(f"no running allocation named {job_name!r}")

    record["leases_used"] += 1
    _write_remote(run, f"{remote_path(run_dir)}/launch.json",
                  json.dumps(record, indent=1, sort_keys=True))
    argv = claude_argv(agent, prompt="Continue where you stopped.", session_id=session_id,
                       settings_path=f"{run_dir}/settings.json",
                       mcp_config_path=f"{run_dir}/mcp.json" if agent.mcp else None,
                       resume=True)
    run(_detached(job.job_id, agent, run_dir, argv))
    log.info("launch.continued", session=session_id, lease=record["leases_used"])
    return session_id


# %%
if test():
    import tempfile

    squeue = "dev|62526|g004|R|03:00:00|00:10:00|gres/gpu=2|\n"
    cluster = ClusterConfig(login_host="h", run_root="~/.slurm-agent/runs")

    blocked = FakeRunner({"squeue --job": "", "squeue": squeue, "test -e": "yes",
                          "test -d": "yes", "status --porcelain": "",
                          "rev-parse": "a1b2c3d4\n", "test -f": "no"})
    try:
        launch(agent, "TASK-104", "dev", blocked, cluster)
        raise AssertionError("missing env should have raised")
    except MissingEnvError as exc:
        assert "HF_TOKEN" in str(exc)
        # NOTHING is submitted when the preflight fails — that is the whole point.
        assert not blocked.asked("srun")
        display(str(exc))


# %%
if test():
    ok = FakeRunner({"squeue --job": "", "squeue": squeue, "test -e": "yes",
                     "test -d": "yes", "status --porcelain": "", "rev-parse": "a1b2c3d4\n",
                     "test -f": "yes", "cat": "export HF_TOKEN=real\n"})
    session = launch(agent, "TASK-104", "dev", ok, cluster)
    assert len(session) == 36

    fired = [c for c in ok.commands if "srun" in c][0]
    assert "setsid nohup srun" in fired and "--overlap" in fired
    assert "SLURM_AGENT_RUN_DIR" in fired
    assert '"$HOME"/.slurm-agent/runs' in fired      # $HOME, never a literal tilde
    assert fired.rstrip().endswith("&")              # detached: returns immediately

    # Every file we wrote is under the run root, none inside the staged repo.
    writes = [c.split(">")[1].split("<<")[0].strip() for c in ok.commands if c.startswith("cat > ")]
    assert writes and all(".slurm-agent/runs" in w for w in writes)
    assert not any("work/baselines" in w for w in writes)
    display(writes)


# %%
if test():
    contended = FakeRunner({"squeue --job": "62526.0|\n62526.1|\n", "squeue": squeue})
    try:
        launch(agent, "TASK-104", "dev", contended, cluster)
        raise AssertionError("a full allocation should have raised")
    except ContentionError as exc:
        assert "agent-batch" in str(exc)
        assert not contended.asked("git clone")      # refused before any staging work
        display(str(exc))


# %% [markdown]
# ## Batch
#
# Tillicum permits one interactive allocation, so anything overnight or running alongside
# a human's session has to be a batch job. Batch jobs are not capped the same way, need
# nobody present, and **end by themselves**: the `claude` run is the script's last
# statement, so the job finishes exactly when the agent's process exits.
#
# Everything downstream is unchanged — same staging, argv, run root, status block, hooks,
# probe and `decide`. Batch is a *submission* difference, not a second system.

# %%
def launch_batch(agent: AgentConfig, task: str, run: Runner, cluster: ClusterConfig, *,
                 exp_id: str | None = None, time_limit: str | None = None,
                 gpus: int = 1, cpus: int = 8, mem: str = "200G") -> tuple[str, str]:
    """Submit the same agent as an sbatch job. Returns (session_id, job_id)."""
    session_id, run_dir, sha = prepare_run(agent, task, run, cluster, exp_id)
    log_dir = resolve_log_dir(agent, run_dir, exp_id or task.lower())
    prompt = launch_prompt(agent, task=task, log_dir=log_dir, run_dir=run_dir, sha=sha)
    argv = claude_argv(
        agent, prompt=prompt, session_id=session_id,
        settings_path=f"{run_dir}/settings.json",
        mcp_config_path=f"{run_dir}/mcp.json" if agent.mcp else None,
    )
    if agent.mcp:
        _write_remote(run, f"{remote_path(run_dir)}/mcp.json", Path("config/mcp.json").read_text())

    script = render(
        "job.sbatch.jinja", session_short=session_id[:8], gpus=gpus, cpus=cpus, mem=mem,
        time_limit=time_limit or agent.batch_time, qos=cluster.default_qos,
        account=cluster.account, run_dir=remote_path(run_dir),
        workdir=remote_path(agent.workdir),
        claude_command=" ".join(quote(a) for a in argv),
    )
    _write_remote(run, f"{remote_path(run_dir)}/job.sbatch", script)
    job_id = run(f"sbatch --parsable {remote_path(run_dir)}/job.sbatch").strip().split(";")[0]

    record = json.loads(run(f"cat {remote_path(run_dir)}/launch.json"))
    record.update({"job_id": job_id, "mode": "batch"})
    _write_remote(run, f"{remote_path(run_dir)}/launch.json",
                  json.dumps(record, indent=1, sort_keys=True))
    log.info("launch.batch", session=session_id, job=job_id, task=task)
    return session_id, job_id


# %%
if test():
    # Ordered: the fake returns the FIRST matching key, so the launch.json read must be
    # matched before the generic `cat` that serves the .envrc read.
    staged = {"cat \"$HOME\"/.slurm-agent": '{"leases_used": 1, "max_leases": 4}',
              "squeue --job": "", "squeue": squeue, "test -e": "yes", "test -d": "yes",
              "status --porcelain": "", "rev-parse": "a1b2c3d4\n", "test -f": "yes",
              "cat": "export HF_TOKEN=real\n", "sbatch": "62999;tillicum\n"}
    batch_runner = FakeRunner(staged)
    session, job_id = launch_batch(agent, "TASK-104", batch_runner, cluster,
                                   time_limit="12:00:00", gpus=2)
    assert job_id == "62999"

    script = [c for c in batch_runner.commands if "job.sbatch <<" in c][0]
    argv_tail = claude_argv(agent, prompt="p", session_id="s",
                            settings_path="x", mcp_config_path="m")[-1]
    assert "#SBATCH --time=12:00:00" in script
    assert "#SBATCH --gpus=2" in script
    assert "SLURM_AGENT_RUN_DIR" in script
    display(script.split("\n")[1:8])


# %%
if test():
    # Self-termination rests entirely on the claude run being the LAST statement, so it is
    # asserted rather than assumed. The prompt is a quoted multi-line argument, so the
    # check is that the script ENDS inside that command: nothing runs after the agent.
    body = script.split("<<'SLURM_AGENT_EOF'\n", 1)[1].rsplit("\nSLURM_AGENT_EOF", 1)[0]
    assert "\nexec claude " in body
    tail = body[body.index("\nexec claude "):].rstrip()
    assert tail.endswith(quote(argv_tail)), tail[-80:]
    display(tail[:80] + " … " + tail[-60:])
