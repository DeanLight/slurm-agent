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
import re
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
# Everything above this line in the template is about the TEMPLATE — that it is committed,
# that a test forbids real values in it. None of that is true of the copy, so the copy gets
# a header written for the copy instead of inheriting one written for something else.
TEMPLATE_BODY_MARK = "# ── keys ──"


# %% [markdown]
# ## Three places, and which one a row is about
#
# Every complaint about setup here has the same shape: *something* is missing, and it is
# not obvious **where**. There are exactly three kinds of place, and a row that does not
# say which one it means is close to useless:
#
# 1. **This laptop** — the checkout you are standing in. Its `.envrc`, its `~/.ssh/config`.
# 2. **The login node** — reachable over ssh. The run root, `tmux`, the Claude credential.
# 3. **One staged repo on the cluster** — an `agents/<kind>.yaml` names a repo, a ref and a
#    workdir, and *that* is how this clone knows which repos it manages. Its `.envrc` is a
#    different file from the laptop's and holds different keys.
#
# So `where` is part of a `Check`, and `render` groups by it. A place with nothing wrong
# still prints its header, because "which repos does this manage?" is answered by reading
# the headers.

# %%
LAPTOP = "this laptop"


def login_node(cluster: ClusterConfig) -> str:
    """The place-label for the login node."""
    return f"the login node · {cluster.login_host}"


def staged_repo(cluster: ClusterConfig, kind: str, agent: AgentConfig) -> str:
    """The place-label for one agent's staged checkout, naming the file that declares it."""
    return (f"staged repo · {cluster.login_host}:{agent.workdir}  "
            f"[agents/{kind}.yaml → {agent.repo}@{agent.ref}]")


class Check(BaseModel):
    """One row of the report. `ok=None` means SKIPPED — never a pass."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool | None
    detail: str
    fix: str | None = None
    # Which of the three places this row is about. Defaults to the laptop because that is
    # where a row with nothing else said about it is true.
    where: str = LAPTOP


def render(checks: list[Check]) -> str:
    """The report, grouped by place. A skipped row renders SKIPPED and never as ok."""
    places: dict[str, list[Check]] = {}
    for check in checks:
        places.setdefault(check.where, []).append(check)

    width = max((len(c.name) for c in checks), default=10)
    lines = []
    for place, rows in places.items():
        lines.append(f"{place}")
        for check in rows:
            mark = "ok" if check.ok else ("SKIPPED" if check.ok is None else "MISSING")
            lines.append(f"  {check.name:<{width}}  {mark:<8} {check.detail}")
            if check.ok is False and check.fix:
                lines.append(f"  {'':<{width}}  {'':<8} fix: {check.fix}")
        lines.append("")
    failed = sum(1 for c in checks if c.ok is False)
    lines.append(f"{len(checks) - failed} ok, {failed} to fix" if failed
                 else f"all {len(checks)} checks pass")
    return "\n".join(lines)


def inventory(cluster: ClusterConfig, manager: ManagerConfig,
              agents: dict[str, AgentConfig]) -> str:
    """What this clone manages, and where each piece lives. Printed before anything runs.

    This is the answer to "how does it know which repos it manages": it reads
    `agents/*.yaml`, one file per agent, and nothing else. Add a file, and it manages
    another repo; there is no registry and nothing remembered between runs.
    """
    lines = [
        "This clone manages:",
        f"  laptop           {Path.cwd()}",
        f"                   .envrc here holds only YOUR keys, for reaching you:",
        f"                   {', '.join(manager.requires_env) or 'none declared'}",
        f"  login node       {cluster.login_host}",
        f"                   run root {cluster.run_root} · allocations held in "
        f"{cluster.allocation_mode}",
    ]
    if not agents:
        lines.append("  staged repos     none — add an agents/<kind>.yaml to manage one")
        return "\n".join(lines)
    lines.append(f"  staged repos     {len(agents)}, one per agents/<kind>.yaml:")
    for kind, agent in agents.items():
        keys = ", ".join(agent.requires_env) or "no keys"
        lines.append(f"    agents/{kind}.yaml")
        lines.append(f"      {agent.repo}@{agent.ref}")
        lines.append(f"      staged at {cluster.login_host}:{agent.workdir}")
        lines.append(f"      .envrc THERE needs: {keys}")
    return "\n".join(lines)


# %%
if test():
    report = render([
        Check(name="ssh config", ok=True, detail="~/.ssh/config has tillicum-login"),
        Check(name="agent creds", ok=False, detail="no credential", fix="see docs/setup.md",
              where="the login node · tillicum-login"),
        Check(name="notify send", ok=None, detail="not attempted (--no-send)"),
    ])
    assert "MISSING" in report and "SKIPPED" in report
    assert "1 to fix" in report
    # A skipped proof must never read like a proof.
    assert "notify send  ok" not in report
    # Every row sits under a heading naming the machine it is about, so "what is missing"
    # is never separable from "where".
    assert LAPTOP in report and "the login node · tillicum-login" in report
    assert report.index(LAPTOP) < report.index("ssh config")
    display(report)


# %%
if test():
    inv = inventory(ClusterConfig(login_host="tillicum-login"),
                    ManagerConfig(requires_env=["SLURM_AGENT_SMTP_HOST"]), {
        "experiment-runner": AgentConfig(repo="DeanLight/baselines", ref="claude/exp14",
                                         workdir="~/work/baselines", log_dir="experiments",
                                         max_budget_usd=8, requires_env=["HF_TOKEN"]),
    })
    # It must name the FILE, because that is the whole answer to "how does it know?".
    assert "agents/experiment-runner.yaml" in inv
    assert "DeanLight/baselines@claude/exp14" in inv
    # And it must say which .envrc HF_TOKEN belongs in — the one on the cluster, not here.
    assert "tillicum-login:~/work/baselines" in inv
    assert "HF_TOKEN" in inv.split(".envrc THERE needs:")[1]
    display(inv)


# %% [markdown]
# ## Creating

# %%
def init(cluster: ClusterConfig, manager: ManagerConfig, agents: dict[str, AgentConfig],
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
        envrc.write_text(_render_template(cluster, manager, agents))
        envrc.chmod(stat.S_IRUSR | stat.S_IWUSR)
        made.append(Check(name=".envrc", ok=True,
                          detail=f"created {envrc} at 0600 with {SECRET_PLACEHOLDER} values",
                          fix=None))

    made.extend(_install_ssh(ssh_dir, cluster, apply=apply))

    if apply:
        try:
            run(f"mkdir -p {remote_path(cluster.run_root)}")
            made.append(Check(name="run root", ok=True, detail=f"{cluster.run_root} ready",
                              where=login_node(cluster)))
        except RemoteError as exc:
            made.append(Check(name="run root", ok=False, detail=str(exc),
                              fix="check `ssh` reaches the login host",
                              where=login_node(cluster)))
    return made


SSH_MARK_START = "# >>> slurm-agent >>>"
SSH_MARK_END = "# <<< slurm-agent <<<"


def _install_ssh(ssh_dir: Path, cluster: ClusterConfig, *, apply: bool = True) -> list[Check]:
    """Add our hosts to ~/.ssh/config without disturbing anything already there.

    Never overwrites. `~/.ssh/config` is a file people keep years of other clusters and
    servers in, so our block is appended between markers and only if the host is not
    already defined — by us or by hand.
    """
    made: list[Check] = []

    node_source = Path("ssh_config_templates") / "tillicum-node-config"
    node_target = ssh_dir / node_source.name
    if node_source.exists():
        if node_target.exists():
            # Ours alone, but `poe job-up` rewrites its Hostname — never clobber that.
            made.append(Check(name="ssh node config", ok=True,
                              detail=f"{node_target} already exists — kept"))
        elif apply:
            ssh_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(node_source, node_target)
            made.append(Check(name="ssh node config", ok=True,
                              detail=f"installed {node_target}"))

    source = Path("ssh_config_templates") / "config"
    target = ssh_dir / "config"
    if not source.exists():
        return made

    existing = target.read_text() if target.exists() else ""
    if re.search(rf"(?im)^\s*host\s+.*\b{re.escape(cluster.login_host)}\b", existing):
        made.append(Check(name="ssh config", ok=True,
                          detail=f"{cluster.login_host} already defined in {target} — untouched"))
        return made

    block = f"\n{SSH_MARK_START}\n{source.read_text().strip()}\n{SSH_MARK_END}\n"
    if apply:
        ssh_dir.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(block)
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    made.append(Check(name="ssh config", ok=True,
                      detail=f"appended {cluster.login_host} to {target} "
                             f"({len(existing.splitlines())} existing lines kept)"))
    return made


def _render_template(cluster: ClusterConfig, manager: ManagerConfig,
                     agents: dict[str, AgentConfig]) -> str:
    """The laptop's `.envrc`: the template's keys, under a header written for THIS file.

    Two things are wrong with copying the template verbatim, and both mislead at exactly
    the moment someone is trying to get set up:

    * its preamble describes the template — committed, no real values, a test enforcing it
      — none of which is true of the copy you are about to put secrets in;
    * it lists every key the repo declares, including keys that are only ever read on the
      cluster. Left live, they look like something to fill in here.

    So the agent-only keys are commented out and annotated with the remote path they
    actually belong in, and the header says which of the three places this file is.
    """
    mine = sorted(set(manager.requires_env))
    body = _template_body() if TEMPLATE.exists() else "\n".join(
        f"{k}={SECRET_PLACEHOLDER}"
        for k in declared_env_keys(manager, list(agents.values()))) + "\n"
    return _copy_header(cluster, mine, agents) + _comment_out_foreign(body, mine)


def _template_body() -> str:
    """The template past its own preamble — the keys, and the comments about the keys."""
    _, mark, body = TEMPLATE.read_text().partition(TEMPLATE_BODY_MARK)
    if not mark:
        return TEMPLATE.read_text()
    return body.partition("\n")[2].lstrip("\n")


def _copy_header(cluster: ClusterConfig, mine: list[str],
                 agents: dict[str, AgentConfig]) -> str:
    """The header for the real file: what it is, where it is read, and what is NOT read."""
    lines = [
        "# YOUR real values. Gitignored, mode 0600, loaded into every `poe` task.",
        "# Written by `poe init` from templates/envrc.example — edit this copy, not that.",
        "#",
        "# This file is read ON THIS LAPTOP ONLY. It holds the keys for reaching you:",
        f"#   {', '.join(mine) or 'none declared in config/manager.yaml'}",
    ]
    foreign = {k: kind for kind, a in agents.items() for k in a.requires_env
               if k not in set(mine)}
    if foreign:
        lines += [
            "#",
            "# Some keys below are COMMENTED OUT because nothing reads them here. Each",
            "# belongs in the .envrc of the repo its agent runs in, on the cluster:",
        ]
        for key, kind in sorted(foreign.items()):
            path = f"{agents[kind].workdir.rstrip('/')}/.envrc"
            lines.append(f"#   {key:<20} {cluster.login_host}:{path}")
        lines += [
            "#",
            "# Filling one in here changes nothing. `poe hc` checks each one where it is",
            "# read, and its report says which machine every row is about.",
        ]
    return "\n".join(lines) + "\n\n"


def _comment_out_foreign(body: str, mine: list[str]) -> str:
    """Comment out any assignment whose key this machine does not read."""
    keep = set(mine)
    out = []
    for line in body.splitlines():
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key and key not in keep:
            out.append(f"# {line}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


# %%
if test():
    import tempfile

    from tests.conftest import FakeRunner

    manager = ManagerConfig(requires_env=["SLURM_AGENT_SMTP_HOST"])
    cluster = ClusterConfig(login_host="h")

    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        runner = FakeRunner()
        init(cluster, manager, {}, runner, envrc=envrc, ssh_dir=Path(tmp) / "ssh")

        assert envrc.exists()
        assert stat.S_IMODE(envrc.stat().st_mode) == 0o600
        assert SECRET_PLACEHOLDER in envrc.read_text()
        assert runner.asked("mkdir -p")
        display(envrc.read_text().splitlines()[-4:])

        # Never overwritten: it is the one file holding irreplaceable human input.
        envrc.write_text("SLURM_AGENT_SMTP_HOST=real.smtp.host\n")
        init(cluster, manager, {}, FakeRunner(), envrc=envrc, ssh_dir=Path(tmp) / "ssh")
        assert envrc.read_text() == "SLURM_AGENT_SMTP_HOST=real.smtp.host\n"


# %%
if test():
    # ~/.ssh/config is where people keep years of other clusters and servers. Our hosts are
    # APPENDED between markers; nothing already there is touched, and nothing is overwritten.
    with tempfile.TemporaryDirectory() as tmp:
        ssh_dir = Path(tmp) / "ssh"
        ssh_dir.mkdir()
        mine = ("Host my-other-cluster\n    Hostname login.elsewhere.edu\n"
                "    User someone\n\nHost bastion\n    Hostname 10.0.0.1\n")
        (ssh_dir / "config").write_text(mine)

        rows = _install_ssh(ssh_dir, ClusterConfig(login_host="tillicum-login"))
        after = (ssh_dir / "config").read_text()

        assert after.startswith(mine)          # every existing line survives, in order
        assert "my-other-cluster" in after and "bastion" in after
        assert SSH_MARK_START in after and "tillicum-login" in after
        assert any("appended" in r.detail for r in rows)
        display(after)


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        ssh_dir = Path(tmp) / "ssh"
        ssh_dir.mkdir()
        # A host the user defined BY HAND is left completely alone — no second block, no
        # duplicate Host stanza fighting theirs.
        hand_rolled = "Host tillicum-login\n    Hostname klone.hyak.uw.edu\n    User me\n"
        (ssh_dir / "config").write_text(hand_rolled)

        rows = _install_ssh(ssh_dir, ClusterConfig(login_host="tillicum-login"))
        assert (ssh_dir / "config").read_text() == hand_rolled
        assert any("untouched" in r.detail for r in rows)

        # And running init twice does not append a second block.
        _install_ssh(ssh_dir, ClusterConfig(login_host="tillicum-login"))
        assert (ssh_dir / "config").read_text().count("Host tillicum-login") == 1
        display(rows[-1].detail)


# %%
if test():
    # The copy must not inherit prose that is only true of the template, and must not
    # present a key it never reads as something to fill in.
    written = _render_template(
        ClusterConfig(login_host="tillicum-login"),
        ManagerConfig(requires_env=["SLURM_AGENT_SMTP_HOST"]),
        {"experiment-runner": AgentConfig(repo="r", ref="main", workdir="~/work/baselines",
                                          log_dir="e", max_budget_usd=1,
                                          requires_env=["HF_TOKEN"])},
    )
    # Template-only prose is gone; a header about THIS file replaces it.
    assert "This file is read ON THIS LAPTOP ONLY" in written
    # Nothing about the template being committed, or about the test that guards it.
    assert "committed" not in written and TEMPLATE_BODY_MARK not in written
    # The manager's key is live; the agent's is commented out and points somewhere real.
    assert "\nSLURM_AGENT_SMTP_HOST=" in written
    assert "\n# HF_TOKEN=" in written
    assert "tillicum-login:~/work/baselines/.envrc" in written
    display(written.split("\n\n")[0])


# %% [markdown]
# ## Checking

# %%
def healthcheck(cluster: ClusterConfig, manager: ManagerConfig,
                agents: dict[str, AgentConfig], run: Runner, *, full: bool = False,
                send: bool = False, envrc: Path | None = None,
                env: dict[str, str] | None = None, ssh_dir: Path | None = None,
                notify_test=None) -> list[Check]:
    """Is everything wired and working? FAST tier by default; `--full` adds the slow proofs.

    Every row carries the place it is about, and the three places do not share keys or
    files. The laptop is asked only for the manager's own keys; an agent's keys are asked
    for in that agent's staged repo on the cluster, which is the only place they are read.
    """
    envrc = envrc or Path(manager.envrc)
    env = os.environ if env is None else env
    login = login_node(cluster)
    checks: list[Check] = []

    # ── FAST · this laptop ───────────────────────────────────────────────────────
    checks.append(_envrc_check(envrc))

    # ONLY the manager's keys. An agent's keys are read on the compute node, out of the
    # .envrc beside the repo it runs in — asking the laptop for an HF token would fail a
    # correctly-configured machine and send you to fill in a file nothing ever reads.
    keys = sorted(set(manager.requires_env))
    absent = missing_env(keys, env)
    elsewhere = sorted({k for a in agents.values() for k in a.requires_env} - set(keys))
    checks.append(Check(
        name="my keys", ok=not absent,
        detail=f"{len(keys) - len(absent)}/{len(keys)} keys for reaching me are set"
               + (f" — missing {', '.join(absent)}" if absent else "")
               + (f" (not checked here: {', '.join(elsewhere)} — those belong in the "
                  "staged repos below)" if elsewhere else ""),
        fix=f"fill them in in {envrc}" if absent else None,
    ))
    checks.append(_ssh_config_check(ssh_dir, cluster))

    # ── FAST · the login node ────────────────────────────────────────────────────
    try:
        who = run("id -un").strip()
        reachable = bool(who)
        # The row `poe hc` exists for: a dropped ControlMaster after a network change is
        # the usual cause of "everything is broken", and it costs one second to rule out.
        checks.append(Check(name="reachable", ok=reachable, where=login,
                            detail=f"answered as {who}" if reachable else "no answer",
                            fix="re-auth: `ssh " + cluster.login_host + "`"))
    except RemoteError as exc:
        reachable = False
        checks.append(Check(name="reachable", ok=False, detail=str(exc), where=login,
                            fix=f"re-auth: `ssh {cluster.login_host}`"))

    if not reachable:
        # One broken link must not render as eight independent problems.
        checks.append(Check(name="run root", ok=None, where=login,
                            detail="skipped: login node unreachable"))
        for kind, agent in agents.items():
            checks.append(Check(name=".envrc", ok=None,
                                where=staged_repo(cluster, kind, agent),
                                detail="skipped: login node unreachable"))
    else:
        checks.append(_remote_exists(run, cluster.run_root, "run root", where=login))
        if cluster.allocation_mode == "tmux":
            checks.append(_tmux(run, where=login))
        # ── FAST · each staged repo ──────────────────────────────────────────────
        checks.extend(_remote_envrc(run, cluster, agents))

    # ── FULL ─────────────────────────────────────────────────────────────────────
    if full and reachable:
        checks.append(_allocation_probe(run, cluster))
        checks.append(_agent_credential(run, where=login))
    elif full:
        checks.append(Check(name="allocation probe", ok=None, where=login,
                            detail="skipped: login node unreachable"))

    # ── SEND ─────────────────────────────────────────────────────────────────────
    if send and notify_test is not None:
        for where, ok, detail in notify_test():
            checks.append(Check(
                name="notify send", ok=ok, detail=detail,
                where=LAPTOP if where == "local" else login,
                fix=None if ok else
                ("check the SMTP/Slack keys in .envrc" if where == "local" else
                 f"the cluster could not send — check python3 on {cluster.login_host}")))
    else:
        checks.append(Check(name="notify send", ok=None,
                            detail="not attempted — run `poe init` or `poe hc --send`"))
    return checks


def _ssh_config_check(ssh_dir: Path | None, cluster: ClusterConfig) -> Check:
    """Is the login host defined in ~/.ssh/config? Local, instant, and the usual first gap.

    `poe init` appends it. Without this row a fresh clone's only symptom is the cluster
    row failing, which reads like a network or auth problem rather than a missing host.
    """
    target = (ssh_dir or Path("~/.ssh").expanduser()) / "config"
    if not target.exists():
        return Check(name="ssh config", ok=False, detail=f"{target} missing",
                     fix="poe init")
    defined = bool(re.search(rf"(?im)^\s*host\s+.*\b{re.escape(cluster.login_host)}\b",
                             target.read_text()))
    return Check(name="ssh config", ok=defined,
                 detail=f"{target} defines {cluster.login_host}" if defined
                 else f"{target} has no {cluster.login_host} host",
                 fix=None if defined else "poe init")


def _envrc_check(envrc: Path) -> Check:
    if not envrc.exists():
        return Check(name=".envrc", ok=False, detail=f"{envrc} missing", fix="poe init")
    mode = stat.S_IMODE(envrc.stat().st_mode)
    if mode & 0o077:
        # A shared filesystem makes a group-readable app-password the real exposure.
        return Check(name=".envrc", ok=False, detail=f"{envrc} is {oct(mode)}",
                     fix=f"chmod 600 {envrc}")
    return Check(name=".envrc", ok=True, detail=f"{envrc} present at 0600")


def _tmux(run: Runner, *, where: str = LAPTOP) -> Check:
    """Is tmux on the login node? Every allocation depends on it in the default mode.

    Fast tier, because it is one command over the connection that is already open — and
    finding out here beats finding out when an allocation silently fails to come up.
    """
    version = run("command -v tmux >/dev/null && tmux -V || echo missing").strip()
    present = version != "missing" and bool(version)
    return Check(
        name="tmux", ok=present, where=where,
        detail=version if present else "not installed",
        fix=None if present else
        "install tmux there, or set allocation_mode: no_shell in config/cluster.yaml "
        "(you lose the ability to attach to a running allocation)",
    )


def _remote_exists(run: Runner, path: str, name: str, *, where: str = LAPTOP) -> Check:
    present = run(f"test -d {remote_path(path)} && echo yes || echo no").strip() == "yes"
    return Check(name=name, ok=present, where=where,
                 detail=f"{path} {'exists' if present else 'missing'}",
                 fix="poe init" if not present else None)


def _remote_envrc(run: Runner, cluster: ClusterConfig,
                  agents: dict[str, AgentConfig]) -> list[Check]:
    """Each staged repo's own `.envrc` on the cluster — mode and the keys IT declares.

    One group per agent, always, even when it declares no keys: the group heading names
    the repo, the ref and the workdir, so reading the report is how you find out which
    repos this clone manages and where each one is staged.
    """
    from slurm_agent.staging import missing_env_remote

    checks = []
    for kind, agent in agents.items():
        where = staged_repo(cluster, kind, agent)
        if not agent.requires_env:
            checks.append(Check(name=".envrc", ok=True, where=where,
                                detail="declares no keys — nothing needed here"))
            continue
        path = f"{agent.workdir.rstrip('/')}/.envrc"
        mode = run(f"stat -c %a {remote_path(path)} 2>/dev/null || echo none").strip()
        if mode == "none":
            checks.append(Check(
                name=".envrc", ok=False, where=where,
                detail=f"{path} missing — needs {', '.join(agent.requires_env)}",
                fix=f"scp templates/envrc.example {cluster.login_host}:{path} "
                    f"&& ssh {cluster.login_host} 'chmod 600 {path}' then fill it in"))
            continue
        secure = mode.endswith("00")
        absent = missing_env_remote(agent, run)
        checks.append(Check(
            name=".envrc", ok=secure and not absent, where=where,
            detail=f"mode {mode}" + (f", missing {', '.join(absent)}" if absent
                                     else ", every declared key set"),
            fix=None if secure and not absent else
            f"ssh {cluster.login_host} 'chmod 600 {path}' and fill in the keys",
        ))
    return checks


def _allocation_probe(run: Runner, cluster: ClusterConfig) -> Check:
    """Does an allocation outlive the ssh that asked for it? Every lease depends on it.

    Probes the mode that is actually configured, rather than assuming --no-shell: under
    `tmux` the question is whether a detached session survives the connection dropping.
    """
    tmux_mode = cluster.allocation_mode == "tmux"
    inner = "salloc --time=00:01:00 --gpus=0 --job-name=sa-probe"
    command = (f"tmux new-session -d -s sa-probe {inner!r} && sleep 2 && "
               "tmux has-session -t sa-probe && echo held" if tmux_mode
               else f"{inner} --no-shell 2>&1 | head -3 || true")
    try:
        out = run(command)
        works = ("held" in out if tmux_mode
                 else "error" not in out.lower() and "invalid" not in out.lower())
        run("tmux kill-session -t sa-probe 2>/dev/null; scancel --name=sa-probe || true")
        other = "no_shell" if tmux_mode else "tmux"
        return Check(name="allocation probe", ok=works, where=login_node(cluster),
                     detail=(f"{cluster.allocation_mode} allocation held after the ssh closed"
                             if works else out.strip()[:120]),
                     fix=None if works else
                     f"try allocation_mode: {other} in config/cluster.yaml")
    except RemoteError as exc:
        return Check(name="allocation probe", ok=False, detail=str(exc)[:120],
                     where=login_node(cluster),
                     fix="try the other allocation_mode in config/cluster.yaml")


def _agent_credential(run: Runner, *, where: str = LAPTOP) -> Check:
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
        return Check(name="agent credential", ok=cost > 0, where=where,
                     detail=f"claude replied, total_cost_usd={cost}",
                     fix=None if cost > 0 else
                     "cost reads 0, so --max-budget-usd is disarmed; use max-turns instead")
    except (RemoteError, ValueError) as exc:
        return Check(name="agent credential", ok=False, detail=str(exc)[:140], where=where,
                     fix="log in to claude on the cluster: `ssh <host> claude`")


# %%
if test():
    good_env = {"SLURM_AGENT_SMTP_HOST": "smtp.x"}
    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        envrc.write_text("SLURM_AGENT_SMTP_HOST=smtp.x\n")
        envrc.chmod(0o600)
        ssh_dir = Path(tmp) / "ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("Host h\n    Hostname h.example\n")
        runner = FakeRunner({"id -un": "deanlcs\n", "test -d": "yes"})
        rows = {c.name: c for c in healthcheck(cluster, manager, {}, runner, envrc=envrc,
                                               env=good_env, ssh_dir=ssh_dir)}
        assert rows[".envrc"].ok and rows["my keys"].ok
        assert rows["reachable"].ok and rows["ssh config"].ok
        # Every row says which of the three places it is about, and the two local rows
        # are not attributed to the cluster.
        assert rows[".envrc"].where == LAPTOP and rows["my keys"].where == LAPTOP
        assert rows["reachable"].where == login_node(cluster)

        # The FAST tier spends nothing: no allocation, no tokens, no messages.
        assert not runner.asked("salloc")
        assert not runner.asked("claude")
        assert rows["notify send"].ok is None
        display(render(list(rows.values())))


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        envrc.write_text("SLURM_AGENT_SMTP_HOST=smtp.x\n")
        envrc.chmod(0o600)

        # tmux is checked in the FAST tier, because every allocation depends on it in the
        # default mode and it is one command over a connection already open.
        has_tmux = FakeRunner({"tmux -V": "tmux 3.3a\n", "id -un": "d\n", "test -d": "yes"})
        rows = {c.name: c for c in healthcheck(cluster, manager, {}, has_tmux,
                                               envrc=envrc, env=good_env)}
        assert rows["tmux"].ok and "3.3a" in rows["tmux"].detail

        no_tmux = FakeRunner({"tmux -V": "missing\n", "id -un": "d\n", "test -d": "yes"})
        rows = {c.name: c for c in healthcheck(cluster, manager, {}, no_tmux,
                                               envrc=envrc, env=good_env)}
        assert rows["tmux"].ok is False
        assert "allocation_mode: no_shell" in rows["tmux"].fix
        display(rows["tmux"].model_dump())

        # Under no_shell there is no tmux dependency, so the row is not raised at all.
        plain = ClusterConfig(login_host="h", allocation_mode="no_shell")
        names = [c.name for c in healthcheck(plain, manager, {}, has_tmux,
                                             envrc=envrc, env=good_env)]
        assert "tmux" not in names


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        # A group-readable .envrc FAILS. On a shared filesystem that is the real exposure.
        loose = Path(tmp) / ".envrc"
        loose.write_text("SLURM_AGENT_SMTP_HOST=smtp.x\n")
        loose.chmod(0o644)
        rows = {c.name: c for c in healthcheck(cluster, manager, {}, FakeRunner(),
                                               envrc=loose, env=good_env)}
        assert rows[".envrc"].ok is False and "chmod 600" in rows[".envrc"].fix

        # A key still holding the placeholder is MISSING, and the report names the key
        # and never a value.
        unfilled = Path(tmp) / "unfilled"
        unfilled.write_text("x\n")
        unfilled.chmod(0o600)
        rows = {c.name: c for c in healthcheck(
            cluster, manager, {}, FakeRunner(), envrc=unfilled,
            env={"SLURM_AGENT_SMTP_HOST": SECRET_PLACEHOLDER})}
        assert rows["my keys"].ok is False
        assert "SLURM_AGENT_SMTP_HOST" in rows["my keys"].detail
        display(rows["my keys"].detail)


# %%
if test():
    # An agent's keys are NOT the laptop's problem. This is the bug the three-place split
    # exists to prevent: a correctly-configured laptop was failing `hc` and being told to
    # put an HF token in a file that nothing ever reads it from.
    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        envrc.write_text("SLURM_AGENT_SMTP_HOST=smtp.x\n")
        envrc.chmod(0o600)
        hungry = {"experiment-runner": AgentConfig(
            repo="DeanLight/baselines", ref="main", workdir="~/work/baselines",
            log_dir="experiments", max_budget_usd=8, requires_env=["HF_TOKEN"])}
        runner = FakeRunner({"id -un": "d\n", "test -d": "yes", "stat -c": "600\n",
                             "cat": "export HF_TOKEN=real\n", "test -f": "yes"})
        rows = healthcheck(cluster, manager, hungry, runner, envrc=envrc, env=good_env)
        mine = next(c for c in rows if c.name == "my keys")
        assert mine.ok, "the laptop has every key IT needs; an agent's key is not one"
        # …but the report still says where HF_TOKEN is expected instead.
        assert "HF_TOKEN" in mine.detail and "staged repos below" in mine.detail
        staged = next(c for c in rows if "staged repo" in c.where)
        assert "agents/experiment-runner.yaml" in staged.where
        assert "~/work/baselines" in staged.where
        display(render(rows))


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

        rows = {c.name: c for c in healthcheck(cluster, manager, {}, _Down(),
                                               envrc=envrc, env=good_env)}
        assert rows["reachable"].ok is False
        assert rows["run root"].ok is None
        display(render(list(rows.values())))

        # --send really sends, and the two sends are attributed to the two machines they
        # left from — the cluster-side one is the path every agent uses, and nothing on
        # the laptop can stand in for it.
        sent = healthcheck(cluster, manager, {}, FakeRunner({"id -un": "d\n", "test -d": "yes"}),
                           envrc=envrc, env=good_env, send=True,
                           notify_test=lambda: [("local", True, "delivered on email"),
                                                ("cluster", True, "ok")])
        sends = [c for c in sent if c.name == "notify send"]
        assert {c.where for c in sends} == {LAPTOP, login_node(cluster)}
        assert all(c.ok for c in sends)
