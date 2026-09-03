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
                        workdir="~/work/baselines", notebook="experiments/{EXP_ID}/run.py",
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
    plain = AgentConfig(repo="r", ref="v", workdir="~/w", notebook="n", max_budget_usd=1)
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


def launch_prompt(agent: AgentConfig, *, task: str, notebook: str, run_dir: str,
                  sha: str) -> str:
    """The launch prompt: the task, the notebook, and the mailbox contract."""
    return render("agent_launch.md.jinja", task=task, repo=agent.repo, ref=agent.ref,
                  sha=sha[:8], workdir=agent.workdir, notebook=notebook,
                  run_dir=run_dir, lease=agent.lease)


def _write_remote(run: Runner, path: str, content: str) -> None:
    """Write a file on the cluster via a quoted heredoc — no scp round trip."""
    run(f"cat > {path} <<'SLURM_AGENT_EOF'\n{content}\nSLURM_AGENT_EOF")


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

    notebook = agent.notebook.replace("{EXP_ID}", exp_id or task.lower())
    abs_notebook = f"{agent.workdir.rstrip('/')}/{notebook}"

    assets = files("slurm_agent") / "assets"
    _write_remote(run, f"{quoted_dir}/remote_status.py",
                  (assets / "remote_status.py").read_text())
    _write_remote(run, f"{quoted_dir}/settings.json", render_asset(
        "hook_settings.json.jinja", run_dir=run_dir, notebook=abs_notebook))
    _write_remote(run, f"{quoted_dir}/launch.json", json.dumps({
        "session_id": session_id, "task": task, "agent": agent.repo, "mode": agent.mode,
        "repo": agent.repo, "ref": agent.ref, "sha": sha, "workdir": agent.workdir,
        "notebook": abs_notebook, "lease": agent.lease, "max_leases": agent.max_leases,
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
    from tests.conftest import FakeRunner

    prompt = launch_prompt(agent, task="TASK-104", notebook="experiments/exp14/run.py",
                           run_dir="/home/d/.slurm-agent/runs/4f2c", sha="a1b2c3d4e5")
    assert "TASK-104" in prompt
    assert "experiments/exp14/run.py" in prompt
    assert "remote_status.py" in prompt
    assert "Dev Workspace" in prompt
    # It must tell the agent NOT to cancel a shared allocation.
    assert "Do not cancel the allocation" in prompt
    display(prompt[:400])


# %%
if test():
    settings = json.loads(render_asset("hook_settings.json.jinja",
                                       run_dir="/run/4f2c", notebook="/w/nb.ipynb"))
    assert set(settings["hooks"]) == {"Stop", "SessionEnd"}
    stop = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "/run/4f2c/remote_status.py tick" in stop and "/w/nb.ipynb" in stop
    display(settings)


# %% [markdown]
# ## Firing it
#
# Detached, so closing the laptop cannot kill it: `setsid nohup` puts the `srun` client in
# its own session on the login node, where it survives the ssh connection dropping.

# %%
def _detached(job_id: str, agent: AgentConfig, run_dir: str, argv: list[str]) -> str:
    """The one-liner that starts an agent as a job step and returns immediately."""
    inner = " ".join(quote(a) for a in argv)
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
           gpus_needed: int = 1) -> str:
    """Stage, preflight and start a Claude agent on an allocation. Returns the session id."""
    job = find_job(job_list(run), job_name)
    if not job or job.state != "R":
        raise LookupError(f"no running allocation named {job_name!r} — try `poe job-up`")

    live = _agents_on(job.job_id, run)
    if job.gpus - len(live) * gpus_needed < gpus_needed:
        raise ContentionError(
            f"allocation {job_name!r} has {job.gpus} gpu and {len(live)} agent(s) on it. "
            "Size it larger, or use `poe agent-batch` — batch jobs are not capped."
        )

    session_id, run_dir, sha = prepare_run(agent, task, run, cluster, exp_id)
    notebook = agent.notebook.replace("{EXP_ID}", exp_id or task.lower())
    prompt = launch_prompt(agent, task=task, notebook=notebook, run_dir=run_dir, sha=sha)
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


def _agents_on(job_id: str, run: Runner) -> list[str]:
    """Step ids already running on this allocation — how many agents are sharing it."""
    out = run(f"squeue --job={quote(job_id)} --steps --noheader --Format=StepID:|")
    return [line.strip().rstrip("|") for line in out.splitlines() if line.strip()]


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
