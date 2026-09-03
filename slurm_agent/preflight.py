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
# # Setting up, and checking
#
# Two commands, because they answer two different questions at two different rhythms.
#
# * **`init` creates.** Run once, wiring a fresh clone to the cluster. It ends by running a
#   full healthcheck *including real sends*, so setting up finishes with a proof rather
#   than an assumption.
# * **`healthcheck` verifies** and creates nothing. Fast by default — seconds, no tokens,
#   no GPU, no messages — so `poe hc` is worth typing every time you move network or
#   re-auth. The single most common "everything is broken" cause is a dropped
#   `ControlMaster`, and that is a one-second check.

# %%
import os
import shutil
import stat
from pathlib import Path

import structlog
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict

from slurm_agent.config import (
    SECRET_PLACEHOLDER,
    AgentConfig,
    ClusterConfig,
    ManagerConfig,
    declared_env_keys,
    load,
    missing_env,
)
from slurm_agent.remote import Runner, RemoteError, remote_path

log = structlog.get_logger(__name__)

TEMPLATE = Path("templates/envrc.example")


# %%
class Check(BaseModel):
    """One row of the report. `ok=None` means SKIPPED — never a pass."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool | None
    detail: str
    fix: str | None = None


def render(checks: list[Check]) -> str:
    """The report. A skipped row renders SKIPPED and never as ok."""
    width = max((len(c.name) for c in checks), default=10)
    lines = []
    for check in checks:
        mark = "ok" if check.ok else ("SKIPPED" if check.ok is None else "MISSING")
        line = f"{check.name:<{width}}  {mark:<8} {check.detail}"
        if check.ok is False and check.fix:
            line += f"\n{'':<{width}}  {'':<8} fix: {check.fix}"
        lines.append(line)
    failed = sum(1 for c in checks if c.ok is False)
    lines.append("")
    lines.append(f"{len(checks) - failed} ok, {failed} to fix" if failed
                 else f"all {len(checks)} checks pass")
    return "\n".join(lines)


# %%
if test():
    report = render([
        Check(name="ssh config", ok=True, detail="~/.ssh/config has tillicum-login"),
        Check(name="agent creds", ok=False, detail="no credential", fix="see docs/setup.md"),
        Check(name="notify send", ok=None, detail="not attempted (--no-send)"),
    ])
    assert "MISSING" in report and "SKIPPED" in report
    assert "1 to fix" in report
    # A skipped proof must never read like a proof.
    assert "notify send  ok" not in report
    display(report)


# %% [markdown]
# ## Creating

# %%
def init(cluster: ClusterConfig, manager: ManagerConfig, agents: list[AgentConfig],
         run: Runner, *, envrc: Path | None = None, ssh_dir: Path | None = None,
         apply: bool = True) -> list[Check]:
    """Create the local footprint. Creates only what is safe, and never a secret value."""
    envrc = envrc or Path(manager.envrc)
    ssh_dir = ssh_dir or Path("~/.ssh").expanduser()
    made: list[Check] = []

    if envrc.exists():
        # The one file here holding irreplaceable human input. Never overwritten.
        made.append(Check(name=".envrc", ok=True, detail=f"{envrc} already exists — kept"))
    elif apply:
        envrc.write_text(_render_template(manager, agents))
        envrc.chmod(stat.S_IRUSR | stat.S_IWUSR)
        made.append(Check(name=".envrc", ok=True,
                          detail=f"created {envrc} at 0600 with {SECRET_PLACEHOLDER} values",
                          fix=None))

    for name in ("config", "tillicum-node-config"):
        source = Path("ssh_config_templates") / name
        target = ssh_dir / name
        if not source.exists():
            continue
        if target.exists():
            made.append(Check(name=f"ssh {name}", ok=True, detail=f"{target} already exists"))
        elif apply:
            ssh_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
            made.append(Check(name=f"ssh {name}", ok=True, detail=f"installed {target}"))

    if apply:
        try:
            run(f"mkdir -p {remote_path(cluster.run_root)}")
            made.append(Check(name="run root", ok=True, detail=f"{cluster.run_root} ready"))
        except RemoteError as exc:
            made.append(Check(name="run root", ok=False, detail=str(exc),
                              fix="check `ssh` reaches the login host"))
    return made


def _render_template(manager: ManagerConfig, agents: list[AgentConfig]) -> str:
    """The env template, or a generated stand-in covering every declared key."""
    if TEMPLATE.exists():
        return TEMPLATE.read_text()
    keys = declared_env_keys(manager, agents)
    return "\n".join(f"{k}={SECRET_PLACEHOLDER}" for k in keys) + "\n"


# %%
if test():
    import tempfile

    from tests.conftest import FakeRunner

    manager = ManagerConfig(requires_env=["SLURM_AGENT_SMTP_HOST"])
    cluster = ClusterConfig(login_host="h")

    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        runner = FakeRunner()
        init(cluster, manager, [], runner, envrc=envrc, ssh_dir=Path(tmp) / "ssh")

        assert envrc.exists()
        assert stat.S_IMODE(envrc.stat().st_mode) == 0o600
        assert SECRET_PLACEHOLDER in envrc.read_text()
        assert runner.asked("mkdir -p")
        display(envrc.read_text().splitlines()[-4:])

        # Never overwritten: it is the one file holding irreplaceable human input.
        envrc.write_text("SLURM_AGENT_SMTP_HOST=real.smtp.host\n")
        init(cluster, manager, [], FakeRunner(), envrc=envrc, ssh_dir=Path(tmp) / "ssh")
        assert envrc.read_text() == "SLURM_AGENT_SMTP_HOST=real.smtp.host\n"


# %% [markdown]
# ## Checking

# %%
def healthcheck(cluster: ClusterConfig, manager: ManagerConfig, agents: list[AgentConfig],
                run: Runner, *, full: bool = False, send: bool = False,
                envrc: Path | None = None, env: dict[str, str] | None = None,
                notify_test=None) -> list[Check]:
    """Is everything wired and working? FAST tier by default; `--full` adds the slow proofs."""
    envrc = envrc or Path(manager.envrc)
    env = os.environ if env is None else env
    checks: list[Check] = []

    # ── FAST ─────────────────────────────────────────────────────────────────────
    checks.append(_envrc_check(envrc))
    keys = declared_env_keys(manager, agents)
    absent = missing_env(keys, env)
    checks.append(Check(
        name="declared env", ok=not absent,
        detail=f"{len(keys) - len(absent)}/{len(keys)} keys set"
               + (f" — missing {', '.join(absent)}" if absent else ""),
        fix=f"fill them in in {envrc}" if absent else None,
    ))

    try:
        who = run("id -un").strip()
        reachable = bool(who)
        # The row `poe hc` exists for: a dropped ControlMaster after a network change is
        # the usual cause of "everything is broken", and it costs one second to rule out.
        checks.append(Check(name="cluster identity", ok=reachable,
                            detail=f"{cluster.login_host} reachable as {who}" if reachable
                            else "no answer", fix="re-auth: `ssh " + cluster.login_host + "`"))
    except RemoteError as exc:
        reachable = False
        checks.append(Check(name="cluster identity", ok=False, detail=str(exc),
                            fix=f"re-auth: `ssh {cluster.login_host}`"))

    if not reachable:
        # One broken link must not render as eight independent problems.
        for name in ("run root", "cluster .envrc"):
            checks.append(Check(name=name, ok=None, detail="skipped: cluster unreachable"))
    else:
        checks.append(_remote_exists(run, cluster.run_root, "run root"))
        checks.extend(_remote_envrc(run, agents))

    # ── FULL ─────────────────────────────────────────────────────────────────────
    if full and reachable:
        checks.append(_allocation_probe(run, cluster))
        checks.append(_agent_credential(run))
    elif full:
        checks.append(Check(name="allocation probe", ok=None, detail="skipped: unreachable"))

    # ── SEND ─────────────────────────────────────────────────────────────────────
    if send and notify_test is not None:
        for where, ok, detail in notify_test():
            checks.append(Check(name=f"notify send ({where})", ok=ok, detail=detail,
                                fix="check the SMTP/Slack keys in .envrc" if not ok else None))
    else:
        checks.append(Check(name="notify send", ok=None,
                            detail="not attempted — run `poe init` or `poe hc --send`"))
    return checks


def _envrc_check(envrc: Path) -> Check:
    if not envrc.exists():
        return Check(name=".envrc", ok=False, detail=f"{envrc} missing", fix="poe init")
    mode = stat.S_IMODE(envrc.stat().st_mode)
    if mode & 0o077:
        # A shared filesystem makes a group-readable app-password the real exposure.
        return Check(name=".envrc", ok=False, detail=f"{envrc} is {oct(mode)}",
                     fix=f"chmod 600 {envrc}")
    return Check(name=".envrc", ok=True, detail=f"{envrc} present at 0600")


def _remote_exists(run: Runner, path: str, name: str) -> Check:
    present = run(f"test -d {remote_path(path)} && echo yes || echo no").strip() == "yes"
    return Check(name=name, ok=present, detail=f"{path} {'exists' if present else 'missing'}",
                 fix="poe init" if not present else None)


def _remote_envrc(run: Runner, agents: list[AgentConfig]) -> list[Check]:
    """Each agent's workdir .envrc on the cluster — mode and declared keys."""
    from slurm_agent.staging import missing_env_remote

    checks = []
    for agent in agents:
        if not agent.requires_env:
            continue
        path = f"{agent.workdir.rstrip('/')}/.envrc"
        mode = run(f"stat -c %a {remote_path(path)} 2>/dev/null || echo none").strip()
        if mode == "none":
            checks.append(Check(name=f"cluster .envrc ({agent.repo.split('/')[-1]})",
                                ok=False, detail=f"{path} missing",
                                fix=f"copy templates/envrc.example to {path}, chmod 600"))
            continue
        secure = mode.endswith("00")
        absent = missing_env_remote(agent, run)
        checks.append(Check(
            name=f"cluster .envrc ({agent.repo.split('/')[-1]})",
            ok=secure and not absent,
            detail=f"{path} mode {mode}" + (f", missing {', '.join(absent)}" if absent else ""),
            fix=None if secure and not absent else f"chmod 600 {path} and fill in the keys",
        ))
    return checks


def _allocation_probe(run: Runner, cluster: ClusterConfig) -> Check:
    """Does an allocation outlive the ssh that asked for it? Every lease depends on it."""
    try:
        out = run("salloc --no-shell --time=00:01:00 --gpus=0 --job-name=sa-probe 2>&1 "
                  "| head -3 || true")
        works = "error" not in out.lower() and "invalid" not in out.lower()
        run("scancel --name=sa-probe || true")
        return Check(name="allocation probe", ok=works,
                     detail="salloc --no-shell accepted" if works else out.strip()[:120],
                     fix=None if works else "set allocation_mode: tmux in config/cluster.yaml")
    except RemoteError as exc:
        return Check(name="allocation probe", ok=False, detail=str(exc)[:120],
                     fix="set allocation_mode: tmux in config/cluster.yaml")


def _agent_credential(run: Runner) -> Check:
    """Headless claude auth on the cluster, AND that it reports a non-zero cost.

    A subscription reporting zero would silently disarm --max-budget-usd, and nothing else
    would ever notice.
    """
    import json as _json

    try:
        raw = run("claude -p 'Reply with exactly: OK' --output-format json "
                  "--max-budget-usd 1 2>/dev/null")
        result = _json.loads(raw[raw.index("{"):])
        cost = float(result.get("total_cost_usd") or 0)
        return Check(name="agent credential", ok=cost > 0,
                     detail=f"claude replied, total_cost_usd={cost}",
                     fix=None if cost > 0 else
                     "cost reads 0, so --max-budget-usd is disarmed; use max-turns instead")
    except (RemoteError, ValueError) as exc:
        return Check(name="agent credential", ok=False, detail=str(exc)[:140],
                     fix="log in to claude on the cluster: `ssh <host> claude`")


# %%
if test():
    good_env = {"SLURM_AGENT_SMTP_HOST": "smtp.x"}
    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        envrc.write_text("SLURM_AGENT_SMTP_HOST=smtp.x\n")
        envrc.chmod(0o600)
        runner = FakeRunner({"id -un": "deanlcs\n", "test -d": "yes"})
        rows = {c.name: c for c in healthcheck(cluster, manager, [], runner,
                                               envrc=envrc, env=good_env)}
        assert rows[".envrc"].ok and rows["declared env"].ok
        assert rows["cluster identity"].ok

        # The FAST tier spends nothing: no allocation, no tokens, no messages.
        assert not runner.asked("salloc")
        assert not runner.asked("claude")
        assert rows["notify send"].ok is None
        display(render(list(rows.values())))


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        # A group-readable .envrc FAILS. On a shared filesystem that is the real exposure.
        loose = Path(tmp) / ".envrc"
        loose.write_text("SLURM_AGENT_SMTP_HOST=smtp.x\n")
        loose.chmod(0o644)
        rows = {c.name: c for c in healthcheck(cluster, manager, [], FakeRunner(),
                                               envrc=loose, env=good_env)}
        assert rows[".envrc"].ok is False and "chmod 600" in rows[".envrc"].fix

        # A key still holding the placeholder is MISSING, and the report names the key
        # and never a value.
        unfilled = Path(tmp) / "unfilled"
        unfilled.write_text("x\n")
        unfilled.chmod(0o600)
        rows = {c.name: c for c in healthcheck(
            cluster, manager, [], FakeRunner(), envrc=unfilled,
            env={"SLURM_AGENT_SMTP_HOST": SECRET_PLACEHOLDER})}
        assert rows["declared env"].ok is False
        assert "SLURM_AGENT_SMTP_HOST" in rows["declared env"].detail
        display(rows["declared env"].detail)


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        envrc.write_text("SLURM_AGENT_SMTP_HOST=smtp.x\n")
        envrc.chmod(0o600)

        # ssh down: cluster rows are SKIPPED, not failed — one broken link must not read
        # as many independent problems.
        class _Down(FakeRunner):
            def __call__(self, command, stdin=None):
                self.commands.append(command)
                raise RemoteError(command, "Connection timed out")

        rows = {c.name: c for c in healthcheck(cluster, manager, [], _Down(),
                                               envrc=envrc, env=good_env)}
        assert rows["cluster identity"].ok is False
        assert rows["run root"].ok is None
        display(render(list(rows.values())))

        # --send really sends, and the rows carry what came back.
        sent = healthcheck(cluster, manager, [], FakeRunner({"id -un": "d\n", "test -d": "yes"}),
                           envrc=envrc, env=good_env, send=True,
                           notify_test=lambda: [("local", True, "delivered on email"),
                                                ("cluster", True, "ok")])
        names = [c.name for c in sent]
        assert "notify send (local)" in names and "notify send (cluster)" in names
        assert all(c.ok for c in sent if c.name.startswith("notify send"))
