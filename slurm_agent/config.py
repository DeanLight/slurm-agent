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
# # Configuration
#
# Every knob this repo has, as pydantic models over YAML files in `config/` and `agents/`.
#
# Two rules hold across all of them:
#
# * **`extra="forbid"` everywhere.** A typo in a config key is a load-time error, not a
#   surprise at launch time.
# * **Committed YAML names environment *keys*, never values.** Secrets live in the
#   gitignored `.envrc`; `requires_env` is how a config says what it needs without ever
#   holding it.

# %%
import os
import re
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict, ValidationError

# %% [markdown]
# ## Durations
#
# SLURM spells a duration `04:00:00`; thresholds read better as `5m`. Both appear in our
# YAML, so both parse.

# %%
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def duration_seconds(text: str | int) -> int:
    """Parse `5m`, `04:00:00`, `90s` or a bare number of seconds into seconds."""
    if isinstance(text, int):
        return text
    text = text.strip()
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 2:
            parts = [0, *parts]
        if len(parts) != 3:
            raise ValueError(f"bad duration {text!r}: expected HH:MM:SS")
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    match = re.fullmatch(r"(\d+)([smhd]?)", text)
    if not match:
        raise ValueError(f"bad duration {text!r}: use 90s, 5m, 4h, 2d or HH:MM:SS")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2) or "s"]


# %%
if test():
    assert duration_seconds("90s") == 90
    assert duration_seconds("5m") == 300
    assert duration_seconds("4h") == 14400
    assert duration_seconds("04:00:00") == 14400
    assert duration_seconds("20:00") == 1200
    assert duration_seconds("0") == 0
    assert duration_seconds(45) == 45

    for bad in ("four hours", "", "5x", "1:2:3:4"):
        try:
            duration_seconds(bad)
            raise AssertionError(f"{bad!r} should have raised")
        except ValueError as exc:
            assert "duration" in str(exc)

    print({t: duration_seconds(t) for t in ("90s", "5m", "4h", "04:00:00")})

# %% [markdown]
# ## The cluster
#
# Everything Tillicum-specific. Klone is not designed for here, but nothing is hard-coded
# either — a second cluster is a second YAML file.

# %%
class ClusterConfig(BaseModel):
    """`config/cluster.yaml` — how to reach the cluster and what an hour on it costs."""

    model_config = ConfigDict(extra="forbid")

    login_host: str
    node_config_path: Path = Path("~/.ssh/tillicum-node-config")
    account: str | None = None
    default_qos: str = "interactive"
    gpu_usd_per_hour: float = 0.90
    run_root: str = "~/.slurm-agent/runs"
    # Whether an allocation can outlive the ssh that asked for it. `poe healthcheck --full`
    # probes this; tmux is the fallback when a site refuses `salloc --no-shell`.
    allocation_mode: Literal["no_shell", "tmux"] = "no_shell"


# %%
if test():
    cluster = ClusterConfig(login_host="tillicum-login")
    assert cluster.gpu_usd_per_hour == 0.90
    assert cluster.allocation_mode == "no_shell"
    display(cluster.model_dump())


# %% [markdown]
# ## The local manager
#
# The mirror of `AgentConfig` for the session running on the laptop. `requires_env` names
# the keys it needs — never their values.

# %%
class ManagerConfig(BaseModel):
    """`config/manager.yaml` — the local session's own settings."""

    model_config = ConfigDict(extra="forbid")

    requires_env: list[str] = []
    envrc: Path = Path(".envrc")


# %%
if test():
    manager = ManagerConfig(requires_env=["SLURM_AGENT_SMTP_PASSWORD"])
    assert manager.requires_env == ["SLURM_AGENT_SMTP_PASSWORD"]
    assert manager.envrc == Path(".envrc")
    display(manager.model_dump())


# %% [markdown]
# ## A remote agent
#
# One file per agent kind in `agents/`. This is the audit surface: reviewing what an agent
# was allowed to do means reading this file.

# %%
class AgentConfig(BaseModel):
    """`agents/<kind>.yaml` — what a remote agent may read, run, reach and spend."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    ref: str
    workdir: str
    notebook: str
    requires_env: list[str] = []
    skills: list[str] = []
    mcp: list[str] = []
    allowed_tools: list[str] = []
    # A runaway guard on list-priced token spend, NOT a bill: under a subscription the real
    # limit is the plan's usage window, which the CLI does not expose.
    max_budget_usd: float
    mode: Literal["interactive", "batch"] = "interactive"
    lease: str = "04:00:00"
    max_leases: int = 4
    batch_time: str = "12:00:00"
    model: str | None = None


# %%
if test():
    agent = AgentConfig(
        repo="DeanLight/deepreasoner-baselines",
        ref="claude/exp14",
        workdir="~/work/deepreasoner-baselines",
        notebook="experiments/{EXP_ID}/run.py",
        requires_env=["HF_TOKEN"],
        max_budget_usd=8,
    )
    assert agent.mode == "interactive"
    assert duration_seconds(agent.lease) == 14400

    try:
        AgentConfig(repo="x", ref="y", workdir="z", notebook="n",
                    max_budget_usd=1, buget_usd=5)
        raise AssertionError("extra key should have raised")
    except ValidationError as exc:
        assert "buget_usd" in str(exc)

    display(agent.model_dump())


# %% [markdown]
# ## Supervision and monitoring
#
# `supervision.yaml` is what "stuck" means, written down, so a kill is a rule firing rather
# than a judgement call. `monitor.yaml` carries cadence and thresholds only — channels and
# recipients live in `notify.yaml`, because the digest is not the only sender.

# %%
class SupervisionConfig(BaseModel):
    """`config/supervision.yaml` — the kill thresholds, named."""

    model_config = ConfigDict(extra="forbid")

    poll_every: str = "5m"
    blocked_for: str = "15m"
    status_stale_for: str = "30m"
    no_progress_for: str = "45m"
    gpu_idle_for: str = "20m"
    renew_when_time_left: str = "10m"
    on_kill: Literal["keep_staged"] = "keep_staged"


class MonitorConfig(BaseModel):
    """`config/monitor.yaml` — how often the usage digest speaks, and about what."""

    model_config = ConfigDict(extra="forbid")

    every_days: int = 3
    only_if_changed: bool = True
    budget_used_pct: int = 80
    idle_gpu_hours: float = 2.0


# %%
if test():
    rules = SupervisionConfig()
    assert duration_seconds(rules.blocked_for) == 900
    assert duration_seconds(rules.no_progress_for) == 2700
    display({k: v for k, v in rules.model_dump().items() if k != "on_kill"})


# %% [markdown]
# ## Loading
#
# One entry point. `load()` is the only place a YAML file becomes a model, so
# `extra="forbid"` is the only validation story the repo needs.

# %%
T = TypeVar("T", bound=BaseModel)


def load(path: Path | str, model: type[T]) -> T:
    """Parse one YAML file into one pydantic model."""
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `poe init` to create the local footprint"
        )
    return model.model_validate(yaml.safe_load(path.read_text()) or {})


# %%
if test():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "cluster.yaml"
        good.write_text("login_host: tillicum-login\ngpu_usd_per_hour: 1.25\n")
        assert load(good, ClusterConfig).gpu_usd_per_hour == 1.25

        typo = Path(tmp) / "typo.yaml"
        typo.write_text("login_host: tillicum-login\nlogin_hosts: other\n")
        try:
            load(typo, ClusterConfig)
            raise AssertionError("typo should have raised")
        except ValidationError as exc:
            assert "login_hosts" in str(exc)

        try:
            load(Path(tmp) / "absent.yaml", ClusterConfig)
            raise AssertionError("missing file should have raised")
        except FileNotFoundError as exc:
            assert "poe init" in str(exc)

        display(load(good, ClusterConfig))


# %% [markdown]
# ## Declared environment keys
#
# The union of every `requires_env` in the repo. `templates/envrc.example` is generated
# from this, and the healthcheck reads it — so declaring a key in one place is enough.

# %%
SECRET_PLACEHOLDER = "<secret-here>"


def declared_env_keys(manager: ManagerConfig, agents: list[AgentConfig]) -> list[str]:
    """Every environment key this repo expects, sorted and de-duplicated."""
    keys = {*manager.requires_env}
    for agent in agents:
        keys.update(agent.requires_env)
    return sorted(keys)


def missing_env(keys: list[str], env: dict[str, str] | None = None) -> list[str]:
    """Which of `keys` are unset or still hold the template placeholder.

    Copying the template is not the same as filling it in, and the two must not look alike
    — so a key still reading `<secret-here>` counts as missing.
    """
    env = os.environ if env is None else env
    return [k for k in keys if not env.get(k) or env[k].strip() == SECRET_PLACEHOLDER]


# %%
if test():
    agent_a = AgentConfig(repo="a", ref="r", workdir="w", notebook="n",
                          max_budget_usd=1, requires_env=["HF_TOKEN", "SHARED"])
    agent_b = AgentConfig(repo="b", ref="r", workdir="w", notebook="n",
                          max_budget_usd=1, requires_env=["SHARED"])
    manager_cfg = ManagerConfig(requires_env=["SLURM_AGENT_SMTP_PASSWORD"])

    keys = declared_env_keys(manager_cfg, [agent_a, agent_b])
    assert keys == ["HF_TOKEN", "SHARED", "SLURM_AGENT_SMTP_PASSWORD"]

    env = {"HF_TOKEN": "hf_real", "SHARED": SECRET_PLACEHOLDER}
    assert missing_env(keys, env) == ["SHARED", "SLURM_AGENT_SMTP_PASSWORD"]
    assert missing_env(keys, {k: "set" for k in keys}) == []

    display({"declared": keys, "missing": missing_env(keys, env)})
