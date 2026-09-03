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
# # Staging
#
# Getting the right code, at the right commit, into the right directory on Tillicum —
# and refusing to launch when it isn't.
#
# Tillicum holds many repos. A launch onto a dirty or wrong tree is how an experiment
# silently measures code nobody reviewed, so **`stage` refuses rather than repairs**: it
# never stashes, never resets away someone's work, and never picks a branch for you.
#
# The environment check is the cheap half of `needs_env`. Running it *before* launch means
# a missing token costs a refusal instead of a GPU-hour.

# %%
import structlog
from IPython.display import display
from juplit import test

from slurm_agent.config import SECRET_PLACEHOLDER, AgentConfig
from slurm_agent.remote import Runner, quote, remote_path

log = structlog.get_logger(__name__)


class DirtyWorkdirError(RuntimeError):
    """The staging directory has uncommitted changes, so we will not launch onto it."""

    def __init__(self, workdir: str, paths: list[str]):
        self.workdir = workdir
        self.paths = paths
        listed = ", ".join(paths[:8]) + (" …" if len(paths) > 8 else "")
        super().__init__(
            f"{workdir} has uncommitted changes: {listed}. "
            "Commit or clean it on Tillicum — this never stashes for you."
        )


class MissingEnvError(RuntimeError):
    """Declared environment keys are absent or unfilled. Names only, never values."""

    def __init__(self, keys: list[str], envrc: str):
        self.keys = keys
        self.envrc = envrc
        super().__init__(
            f"missing {', '.join(keys)} — add them to {envrc} and re-run. "
            "This repo never writes secrets for you."
        )


# %%
def stage(agent: AgentConfig, run: Runner) -> str:
    """Clone or fetch `agent.repo` at `agent.ref` into `agent.workdir`. Returns the SHA."""
    workdir = remote_path(agent.workdir)
    if _is_absent(workdir, run):
        run(f"git clone --branch {quote(agent.ref)} "
            f"https://github.com/{quote(agent.repo)}.git {workdir}")
    else:
        if not _is_repo(workdir, run):
            raise ValueError(
                f"{agent.workdir} exists on the cluster but is not a git repo — "
                "move it aside or point workdir somewhere else"
            )
        run(f"git -C {workdir} fetch origin {quote(agent.ref)}")
        dirty = [line[3:] for line in
                 run(f"git -C {workdir} status --porcelain").splitlines() if line.strip()]
        if dirty:
            raise DirtyWorkdirError(agent.workdir, dirty)
        run(f"git -C {workdir} checkout --detach FETCH_HEAD")
    sha = run(f"git -C {workdir} rev-parse HEAD").strip()
    log.info("stage.ready", repo=agent.repo, ref=agent.ref, sha=sha[:8])
    return sha


def _is_absent(workdir: str, run: Runner) -> bool:
    return run(f"test -e {workdir} && echo yes || echo no").strip() == "no"


def _is_repo(workdir: str, run: Runner) -> bool:
    return run(f"test -d {workdir}/.git && echo yes || echo no").strip() == "yes"


# %%
if test():
    from tests.conftest import FakeRunner

    agent = AgentConfig(repo="DeanLight/deepreasoner-baselines", ref="claude/exp14",
                        workdir="~/work/baselines", notebook="experiments/run.py",
                        max_budget_usd=8, requires_env=["HF_TOKEN", "OPENAI_BASE_URL"])

    fresh = FakeRunner({"test -e": "no", "rev-parse": "a1b2c3d4\n"})
    assert stage(agent, fresh) == "a1b2c3d4"
    assert fresh.asked("git clone --branch claude/exp14")
    # The workdir is interpolated with $HOME, never a literal tilde.
    assert fresh.asked('"$HOME"/work/baselines')
    display([c for c in fresh.commands if "clone" in c])


# %%
if test():
    existing = FakeRunner({"test -e": "yes", "test -d": "yes",
                           "status --porcelain": "", "rev-parse": "beefcafe\n"})
    assert stage(agent, existing) == "beefcafe"
    assert existing.asked("fetch origin claude/exp14")
    assert existing.asked("checkout --detach FETCH_HEAD")
    assert not existing.asked("clone")


# %%
if test():
    dirty = FakeRunner({"test -e": "yes", "test -d": "yes",
                        "status --porcelain": " M configs/exp14.yaml\n?? scratch.py\n"})
    try:
        stage(agent, dirty)
        raise AssertionError("a dirty tree should have raised")
    except DirtyWorkdirError as exc:
        assert "configs/exp14.yaml" in str(exc)
        assert not dirty.asked("checkout")        # nothing is touched on refusal
        display(str(exc))

    not_a_repo = FakeRunner({"test -e": "yes", "test -d": "no"})
    try:
        stage(agent, not_a_repo)
        raise AssertionError("a non-repo directory should have raised")
    except ValueError as exc:
        assert "not a git repo" in str(exc)


# %% [markdown]
# ## The environment preflight
#
# Which declared keys are missing from the workdir's `.envrc` on the cluster. **Names
# only** — this never reads a value, and never writes one.

# %%
def missing_env_remote(agent: AgentConfig, run: Runner) -> list[str]:
    """Which of `agent.requires_env` are absent from the workdir's `.envrc` on Tillicum.

    Greps for the key names and for the template placeholder, so a copied-but-unfilled
    `.envrc` reports as missing rather than as configured. Values never leave the cluster.
    """
    if not agent.requires_env:
        return []
    envrc = f"{remote_path(agent.workdir)}/.envrc"
    if run(f"test -f {envrc} && echo yes || echo no").strip() == "no":
        return list(agent.requires_env)

    found = []
    for line in run(f"cat {envrc}").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        if key.strip() in agent.requires_env and value.strip().strip("\"'") != SECRET_PLACEHOLDER:
            found.append(key.strip())
    return [k for k in agent.requires_env if k not in found]


# %%
if test():
    filled = FakeRunner({"test -f": "yes",
                         "cat": "export HF_TOKEN=hf_real\nOPENAI_BASE_URL=https://x\n"})
    assert missing_env_remote(agent, filled) == []

    partial = FakeRunner({"test -f": "yes", "cat": "export HF_TOKEN=hf_real\n"})
    assert missing_env_remote(agent, partial) == ["OPENAI_BASE_URL"]

    # Copied but not filled in is MISSING, not configured — the two must not look alike.
    unfilled = FakeRunner({"test -f": "yes",
                           "cat": f"HF_TOKEN={SECRET_PLACEHOLDER}\nOPENAI_BASE_URL=https://x\n"})
    assert missing_env_remote(agent, unfilled) == ["HF_TOKEN"]

    absent = FakeRunner({"test -f": "no"})
    assert missing_env_remote(agent, absent) == ["HF_TOKEN", "OPENAI_BASE_URL"]

    display({
        "filled": missing_env_remote(agent, filled),
        "unfilled placeholder": missing_env_remote(agent, unfilled),
        "no .envrc": missing_env_remote(agent, absent),
    })


# %%
if test():
    # The error names the keys and the file to edit, and no value ever appears in it.
    try:
        raise MissingEnvError(["HF_TOKEN"], "~/work/baselines/.envrc")
    except MissingEnvError as exc:
        assert "HF_TOKEN" in str(exc) and "never writes secrets" in str(exc)
        display(str(exc))
