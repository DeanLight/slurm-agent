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
import io
import json
import os
import re
import shutil
import stat
from pathlib import Path

import structlog
from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.tree import Tree
from rich.text import Text
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
from slurm_agent.notify import NotifyConfig
from slurm_agent.tasks import TaskConfig
from slurm_agent.remote import Runner, RemoteError, local_runner, quote, remote_path

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
    """The place-label for one agent's staged checkout, naming the file that declares it.

    Square brackets are deliberately absent: these labels are rendered as rich markup, and
    `[agents/x.yaml]` would be swallowed as a style tag — the heading would silently lose
    the one piece of it that says which file to edit.
    """
    return (f"staged repo · agents/{kind}.yaml\n"
            f"{cluster.login_host}:{agent.workdir} · {agent.repo}@{agent.ref}")


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


MARKS = {True: ("[green]ok[/]", "ok"),
         False: ("[bold red]MISSING[/]", "MISSING"),
         None: ("[yellow]SKIPPED[/]", "SKIPPED")}


def _console(**kwargs) -> Console:
    return Console(**kwargs)


def _export(renderable) -> str:
    """The same renderable as plain text — for tests, logs, and anything not a terminal."""
    buffer = io.StringIO()
    _console(file=buffer, width=110, no_color=True, force_terminal=False,
             highlight=False, soft_wrap=False).print(renderable)
    return buffer.getvalue().rstrip("\n")


def report(checks: list[Check]):
    """One table per place, in the order the places were first seen.

    Grouping is the whole point: a row that does not say which machine it is about sends
    people to fix the wrong file. The place is a table title rather than a column, so it is
    said once and cannot be missed, and an empty place still gets its heading.
    """
    places: dict[str, list[Check]] = {}
    for check in checks:
        places.setdefault(check.where, []).append(check)

    blocks = []
    for place, rows in places.items():
        # The heading is its own block, not the table's `title`: a title is wrapped to the
        # table's width, which is set by the content, so a long path would fold into
        # nonsense while there was plenty of console to the right of it.
        head, _, rest = place.partition("\n")
        blocks.append(Text.from_markup(f"[bold cyan]{head}[/]"))
        if rest:
            blocks.append(Text.from_markup(f"[dim]{rest}[/]"))
        table = Table(box=None, show_header=False, pad_edge=False, show_edge=False,
                      expand=False, padding=(0, 1))
        table.add_column("", width=8, justify="left")   # the mark
        table.add_column("", style="bold", no_wrap=True)
        table.add_column("", overflow="fold")
        for check in rows:
            detail = check.detail
            if check.ok is False and check.fix:
                detail += f"\n[dim]fix:[/] {check.fix}"
            table.add_row(MARKS[check.ok][0], check.name, detail)
        blocks.append(table)
        blocks.append(Text(""))

    failed = sum(1 for c in checks if c.ok is False)
    skipped = sum(1 for c in checks if c.ok is None)
    if failed:
        tail = (f"[bold red]{failed} to fix[/], {len(checks) - failed - skipped} ok"
                + (f", {skipped} skipped" if skipped else ""))
    else:
        tail = (f"[bold green]all {len(checks)} checks pass[/]" if not skipped
                else f"[green]{len(checks) - skipped} ok[/], "
                     f"[yellow]{skipped} skipped — a skip is not a pass[/]")
    blocks.append(Text.from_markup(tail))
    return Group(*blocks)


def render(checks: list[Check]) -> str:
    """The report as plain text. A skipped row renders SKIPPED and never as ok."""
    return _export(report(checks))


def print_report(checks: list[Check]) -> None:
    """The report, in colour, to the terminal."""
    _console().print(report(checks))


# %% [markdown]
# ## Creating

# %%
def init(cluster: ClusterConfig, manager: ManagerConfig, agents: dict[str, AgentConfig],
         run: Runner, *, envrc: Path | None = None, ssh_dir: Path | None = None,
         apply: bool = True) -> dict[tuple[str, str], str]:
    """Create the local footprint, and return a NOTE per thing it touched.

    Notes, not checks. Creating and checking the same three files produced two reports that
    said the same things in different words — and worse, a creation failure got reported
    twice: once as a wall of ssh output, and once, correctly, as the check that follows it
    saying the login node is unreachable. So this reports nothing itself. It attempts, and
    what actually exists afterwards is `healthcheck`'s to say; the note only adds the one
    fact a check cannot know, which is whether the thing was already there.
    """
    envrc = envrc or Path(manager.envrc)
    ssh_dir = ssh_dir or Path("~/.ssh").expanduser()
    # Keyed by (place, row), never by row alone: three different machines each have a file
    # called `.envrc`, and a note that found the wrong one would claim a cluster-side file
    # had just been created here.
    notes: dict[tuple[str, str], str] = {}

    if envrc.exists():
        # The one file here holding irreplaceable human input. Never overwritten.
        notes[(LAPTOP, ".envrc")] = "already existed — kept as it was"
    elif apply:
        envrc.write_text(_render_template(cluster, manager, agents))
        envrc.chmod(stat.S_IRUSR | stat.S_IWUSR)
        notes[(LAPTOP, ".envrc")] = (f"just created, with {SECRET_PLACEHOLDER} values to "
                                     "fill in")

    notes.update({(LAPTOP, name): note
                  for name, note in _install_ssh(ssh_dir, cluster, apply=apply).items()})

    if apply:
        try:
            run(f"mkdir -p {remote_path(cluster.run_root)}")
            notes[(login_node(cluster), "run root")] = "just created"
        except RemoteError:
            # Deliberately silent. The `run root` check runs seconds later against the same
            # login node and will say either that it is missing or that the node is
            # unreachable — saying it here too, in ssh's own words, is the noise.
            pass
    return notes


SSH_MARK_START = "# >>> slurm-agent >>>"
SSH_MARK_END = "# <<< slurm-agent <<<"


def _install_ssh(ssh_dir: Path, cluster: ClusterConfig, *,
                 apply: bool = True) -> dict[str, str]:
    """Add our hosts to ~/.ssh/config without disturbing anything already there.

    Never overwrites. `~/.ssh/config` is a file people keep years of other clusters and
    servers in, so our block is appended between markers and only if the host is not
    already defined — by us or by hand.
    """
    notes: dict[str, str] = {}

    node_source = Path("ssh_config_templates") / "tillicum-node-config"
    node_target = ssh_dir / node_source.name
    if node_source.exists() and not node_target.exists() and apply:
        ssh_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(node_source, node_target)
        notes["ssh config"] = f"just installed {node_target.name}"

    source = Path("ssh_config_templates") / "config"
    target = ssh_dir / "config"
    if not source.exists():
        return notes

    existing = target.read_text() if target.exists() else ""
    if re.search(rf"(?im)^\s*host\s+.*\b{re.escape(cluster.login_host)}\b", existing):
        notes["ssh config"] = f"{cluster.login_host} was already defined — left untouched"
        return notes

    block = f"\n{SSH_MARK_START}\n{source.read_text().strip()}\n{SSH_MARK_END}\n"
    if apply:
        ssh_dir.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(block)
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    kept = len(existing.splitlines())
    notes["ssh config"] = ("just appended" if not kept else
                           f"just appended, keeping the {kept} lines already there")
    return notes


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

        notes = _install_ssh(ssh_dir, ClusterConfig(login_host="tillicum-login"))
        after = (ssh_dir / "config").read_text()

        assert after.startswith(mine)          # every existing line survives, in order
        assert "my-other-cluster" in after and "bastion" in after
        assert SSH_MARK_START in after and "tillicum-login" in after
        assert "appended" in notes["ssh config"]
        assert "6 lines already there" in notes["ssh config"]
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

        notes = _install_ssh(ssh_dir, ClusterConfig(login_host="tillicum-login"))
        assert (ssh_dir / "config").read_text() == hand_rolled
        assert "untouched" in notes["ssh config"]

        # And running init twice does not append a second block.
        _install_ssh(ssh_dir, ClusterConfig(login_host="tillicum-login"))
        assert (ssh_dir / "config").read_text().count("Host tillicum-login") == 1
        display(notes["ssh config"])


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
                notify: "NotifyConfig | None" = None, notify_test=None,
                local: Runner | None = None, tasks: "TaskConfig | None" = None,
                created: dict[tuple[str, str], str] | None = None) -> list[Check]:
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
    #
    # And only the keys the channels you turned ON need. Which keys those are is derived
    # from config/notify.yaml rather than listed by hand, so turning a channel off stops
    # its key being demanded, and turning one on starts — the direction that matters, since
    # a hand-maintained list would pass while the escalation silently never arrived.
    from slurm_agent import notify as notifier

    keys = sorted(set(manager.requires_env) | set(notifier.required_keys(notify)))
    absent = missing_env(keys, env)
    defaulted = [k for k in notifier.defaulted_keys(notify) if not env.get(k)]
    off = sorted(set(notifier.all_keys()) - set(keys) - set(defaulted))
    elsewhere = sorted({k for a in agents.values() for k in a.requires_env} - set(keys))
    channels = ", ".join(notify.channels) if notify else "none configured"
    checks.append(Check(
        name="my keys", ok=not absent,
        detail=f"{len(keys) - len(absent)}/{len(keys)} set for channels: {channels}"
               + (f" — missing {', '.join(absent)}" if absent else "")
               + (f"\n[dim]defaulted:[/] {', '.join(defaulted)} unset, using the built-in "
                  f"default ({notifier.SMTP_PORT_DEFAULT} for the SMTP port)"
                  if defaulted else "")
               + (f"\n[dim]not needed:[/] {', '.join(off)} — for a channel "
                  "config/notify.yaml does not enable" if off else "")
               + (f"\n[dim]not checked here:[/] {', '.join(elsewhere)} — those belong in "
                  "the staged repos below" if elsewhere else ""),
        fix=f"fill them in in {envrc}" if absent else None,
    ))
    checks.append(_ssh_config_check(ssh_dir, cluster))

    # Git auth, from HERE. The two machines hold different credentials, and the laptop's
    # is the one you will notice — the login node's is the one that decides whether an
    # agent can push the notebook a GPU-hour produced.
    repos = sorted({a.repo for a in agents.values()})
    checks.extend(_github_access(local or local_runner(), repos, LAPTOP, push=full))

    # ── FAST · the login node ────────────────────────────────────────────────────
    try:
        who = run("id -un").strip()
        reachable = bool(who)
        # The row `poe hc` exists for: a dropped ControlMaster after a network change is
        # the usual cause of "everything is broken", and it costs one second to rule out.
        checks.append(Check(name="reachable", ok=reachable, where=login,
                            detail=f"answered as {who}" if reachable else "no answer",
                            fix=f"open a terminal and run `ssh {cluster.login_host}`, "
                                "answer 2FA, and leave that session open"))
    except RemoteError as exc:
        reachable = False
        checks.append(Check(name="reachable", ok=False, where=login,
                            detail=ssh_reason(str(exc)),
                            fix=f"open a terminal and run `ssh {cluster.login_host}`, "
                                "answer 2FA, and leave that session open"))

    if not reachable:
        # One broken link must not render as eight independent problems.
        checks.append(Check(name="run root", ok=None, where=login,
                            detail="skipped: login node unreachable"))
        for repo in repos:
            checks.append(Check(name=f"github {repo}", ok=None, where=login,
                                detail="skipped: login node unreachable"))
        for kind, agent in agents.items():
            checks.append(Check(name=".envrc", ok=None,
                                where=staged_repo(cluster, kind, agent),
                                detail="skipped: login node unreachable"))
    else:
        checks.append(_remote_exists(run, cluster.run_root, "run root", where=login))
        if cluster.allocation_mode == "tmux":
            checks.append(_tmux(run, where=login))
        checks.extend(_github_access(run, repos, login, push=full))
        # ── FAST · each staged repo ──────────────────────────────────────────────
        checks.extend(_remote_envrc(run, cluster, agents))

    # ── FULL ─────────────────────────────────────────────────────────────────────
    if full:
        # Claude on BOTH machines, for the same reason git is checked on both: the manager
        # runs here and the agents run there, under different credentials. A manager that
        # cannot think is as stuck as an agent that cannot start, and finding out at launch
        # wastes the allocation that was brought up for it.
        checks.append(_agent_credential(local or local_runner(timeout=120), where=LAPTOP))
        # Notion, because a launch with no task id is not allowed and `poe task-new` is how
        # one is got. Failing here costs a few cents; failing at launch costs the
        # allocation that was brought up for the work.
        if tasks is not None:
            checks.append(_task_database(local or local_runner(timeout=300), manager, tasks))
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
    return _annotate(checks, created)


def _annotate(checks: list[Check],
              created: dict[tuple[str, str], str] | None) -> list[Check]:
    """Fold `poe init`'s notes into the rows they are about.

    This is what makes one report instead of two. A note is the only thing a check cannot
    work out for itself — whether the file it is looking at was already there or was put
    there a second ago — so it belongs inside that row rather than in a section above it
    restating the same three filenames.
    """
    if not created:
        return checks
    for check in checks:
        note = created.get((check.where, check.name))
        if note:
            check.detail += f"\n[dim]· {note}[/]"
    return checks


# ssh is loud in the one case you least want noise: a failed auth repeats an askpass
# warning once per attempt, then states the actual reason last. Reporting all of it buries
# "you are not logged in" under four lines of a missing X11 binary that is not the problem
# and that installing would not fix.
SSH_REASONS = (
    ("permission denied", "not authenticated"),
    ("could not resolve", "host not found — check login_host in config/cluster.yaml"),
    ("connection refused", "connection refused"),
    ("connection timed out", "timed out — are you on a network that can reach it?"),
    ("timed out after", "timed out"),
    ("no route to host", "no route to host"),
    ("operation timed out", "timed out"),
)


def ssh_reason(detail: str) -> str:
    """One line for why ssh failed, out of however many ssh chose to print."""
    lowered = detail.lower()
    for needle, reason in SSH_REASONS:
        if needle in lowered:
            return reason
    # Unrecognised: keep ssh's own last meaningful line rather than inventing a summary,
    # but drop the askpass repetition, which is never the cause.
    lines = [ln.strip() for ln in detail.splitlines()
             if ln.strip() and not ln.strip().startswith("ssh_askpass:")]
    return (lines[-1] if lines else detail.strip())[:120]


def _ssh_config_check(ssh_dir: Path | None, cluster: ClusterConfig) -> Check:
    """Is the login host defined in ~/.ssh/config? Local, instant, and the usual first gap.

    `poe init` appends it. Without this row a fresh clone's only symptom is the cluster
    row failing, which reads like a network or auth problem rather than a missing host.
    """
    ssh_dir = ssh_dir or Path("~/.ssh").expanduser()
    target = ssh_dir / "config"
    if not target.exists():
        return Check(name="ssh config", ok=False, detail=f"{target} missing",
                     fix="poe init")
    defined = bool(re.search(rf"(?im)^\s*host\s+.*\b{re.escape(cluster.login_host)}\b",
                             target.read_text()))
    # The node config is `Include`d by the main one, so a missing node file breaks ssh
    # just as surely as a missing host block. One row, because it is one question.
    node = ssh_dir / "tillicum-node-config"
    node_ok = node.exists() or not (Path("ssh_config_templates") / node.name).exists()
    return Check(name="ssh config", ok=defined and node_ok,
                 detail=(f"{target} defines {cluster.login_host}" if defined
                         else f"{target} has no {cluster.login_host} host")
                        + ("" if node_ok else f", but {node} is missing"),
                 fix=None if defined and node_ok else "poe init")


def _envrc_check(envrc: Path) -> Check:
    # Absolute, because this row is the one that says WHICH checkout the report is about —
    # the alternative was a heading repeating a path this row already carries.
    shown = envrc if envrc.is_absolute() else Path.cwd() / envrc
    if not envrc.exists():
        return Check(name=".envrc", ok=False, detail=f"{shown} missing", fix="poe init")
    mode = stat.S_IMODE(envrc.stat().st_mode)
    if mode & 0o077:
        # A shared filesystem makes a group-readable app-password the real exposure.
        return Check(name=".envrc", ok=False, detail=f"{shown} is {oct(mode)}",
                     fix=f"chmod 600 {envrc}")
    return Check(name=".envrc", ok=True, detail=f"{shown} present at 0600")


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


def _github_access(runner: Runner, repos: list[str], where: str, *,
                   push: bool = False) -> list[Check]:
    """Can git reach and authenticate to each repo FROM HERE — wherever "here" is.

    This has to be asked of both machines, because they hold different credentials and
    only one of them is the one that matters at the moment it matters. The laptop's is
    what you notice immediately; the login node's is what an agent needs at the end of a
    GPU-hour, to push the notebook that was the whole point of the run.

    `ls-remote` proves read and authentication. On a PUBLIC repo it succeeds with no
    credential at all, so it cannot prove push — and the row says so rather than implying
    a guarantee it does not have. `push=True` adds the real thing: a `--dry-run` push,
    which authenticates and is authorised by the server and then writes nothing.
    """
    checks = []
    for repo in repos:
        url = f"https://github.com/{repo}.git"
        name = f"github {repo}"
        try:
            out = runner("GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true "
                         f"git ls-remote --heads {quote(url)} 2>&1 | head -3")
        except RemoteError as exc:
            checks.append(Check(name=name, ok=False, where=where, detail=str(exc)[:160],
                                fix=f"authenticate git to github here: `gh auth login`, "
                                    f"or a PAT in git's credential store"))
            continue
        refs = [line for line in out.splitlines() if "\trefs/" in line]
        if not refs:
            checks.append(Check(
                name=name, ok=False, where=where,
                detail=out.strip()[:160] or "no refs and no error — git said nothing",
                fix="authenticate git to github here: `gh auth login`, or a PAT in git's "
                    "credential store"))
            continue
        checks.append(Check(name=name, ok=True, where=where,
                            detail=f"readable, {len(refs)} "
                                   f"branch{'es' if len(refs) != 1 else ''} "
                                   "[dim](read only — a public repo answers this without "
                                   "a credential)[/]"))
        if push:
            checks.append(_push_probe(runner, url, repo, where))
    return checks


def _push_probe(runner: Runner, url: str, repo: str, where: str) -> Check:
    """Prove WRITE access without writing: an authorised `--dry-run` push.

    An agent that cannot push has done its whole run for nothing, and read access does not
    imply write. `--dry-run` performs the connection, the authentication and the server's
    authorisation, then stops before sending a single object — so this is the real answer
    and it still creates no branch.
    """
    probe = ("tmp=$(mktemp -d) && git -C \"$tmp\" init -q "
             "&& git -C \"$tmp\" -c user.email=probe@localhost -c user.name=probe "
             "commit -q --allow-empty -m probe "
             "&& GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true git -C \"$tmp\" push --dry-run "
             f"{quote(url)} HEAD:refs/heads/slurm-agent-push-probe 2>&1 | tail -3; "
             "rm -rf \"$tmp\"")
    name = f"github {repo} push"
    try:
        out = runner(probe)
    except RemoteError as exc:
        out = str(exc)
    denied = any(word in out.lower() for word in
                 ("denied", "403", "not authorized", "authentication", "could not read"))
    return Check(name=name, ok=not denied, where=where,
                 detail="write access confirmed [dim](dry run — nothing was pushed)[/]"
                 if not denied else out.strip()[:160],
                 fix=None if not denied else
                 f"grant this machine push access to {repo}: `gh auth login` with the "
                 "repo scope, or a PAT in git's credential store")


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
        workdir = remote_path(agent.workdir)

        # Is it even there? A workdir is created by the FIRST launch, not by setup — so
        # "not cloned yet" is the normal state of a fresh clone and must not read as a
        # fault. Saying nothing about it was worse: the .envrc row told you to scp a file
        # into a directory that did not exist.
        state = run(f"test -d {workdir}/.git && echo repo "
                    f"|| {{ test -e {workdir} && echo other || echo absent; }}").strip()
        if state == "absent":
            checks.append(Check(
                name="clone", ok=True, where=where,
                detail="not cloned yet — the first `poe agent-run`/`agent-batch` clones it"))
        elif state == "other":
            checks.append(Check(
                name="clone", ok=False, where=where,
                detail=f"{agent.workdir} exists but is not a git repo",
                fix=f"move it aside, or point workdir elsewhere in agents/{kind}.yaml"))
        else:
            checks.extend(_staged_clone(run, cluster, agent, kind, workdir, where))

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
                fix=(f"ssh {cluster.login_host} 'mkdir -p {agent.workdir}' first if it is "
                     f"not cloned yet, then scp templates/envrc.example "
                     f"{cluster.login_host}:{path} && chmod 600 it there")))
            continue
        secure = mode.endswith("00")
        absent = missing_env_remote(agent, run)
        checks.append(Check(
            name=".envrc", ok=secure and not absent, where=where,
            detail=f"mode {mode}" + (f", missing {', '.join(absent)}" if absent
                                     else f", set: {', '.join(agent.requires_env)}"),
            fix=None if secure and not absent else
            f"ssh {cluster.login_host} 'chmod 600 {path}' and fill in the keys",
        ))
    return checks


def _staged_clone(run: Runner, cluster: ClusterConfig, agent: AgentConfig, kind: str,
                  workdir: str, where: str) -> list[Check]:
    """A clone that IS there: does it point at the configured repo, and is it clean?

    Both matter before a launch rather than during one. A workdir left pointing at an
    older `repo:` silently stages the wrong code, and `stage()` refuses a dirty tree — so
    a `poe hc` that did not mention it sends you to a GPU-hour that gets refused instead.
    """
    origin = run(f"git -C {workdir} remote get-url origin 2>/dev/null || echo none").strip()
    matches = agent.repo in origin
    rows = [Check(
        name="clone", ok=matches, where=where,
        detail=f"cloned from {origin}" if matches
        else f"cloned from {origin}, but agents/{kind}.yaml says {agent.repo}",
        fix=None if matches else
        f"ssh {cluster.login_host} 'rm -rf {agent.workdir}' and let the next launch "
        f"re-clone it, or fix repo: in agents/{kind}.yaml")]

    dirty = [ln[3:] for ln in
             run(f"git -C {workdir} status --porcelain").splitlines() if ln.strip()]
    rows.append(Check(
        name="worktree", ok=not dirty, where=where,
        detail="clean" if not dirty
        else f"{len(dirty)} uncommitted: {', '.join(dirty[:4])}",
        fix=None if not dirty else
        f"commit or clean it on {cluster.login_host} — a launch refuses a dirty tree, and "
        "this never stashes for you"))
    return rows


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


def _task_database(run: Runner, manager: ManagerConfig, cfg: "TaskConfig") -> Check:
    """Can the manager actually reach the Tasks database from here?

    Two things at once, and they fail the same way from the outside: the Notion MCP being
    reachable and authenticated, and `data_source` in `config/tasks.yaml` naming a database
    that exists. Proving them together is enough, because the fix for either is a sentence.

    It goes through the same `manager_argv` a real ask uses, so this proves the path the
    work will take rather than a similar-looking one.
    """
    from slurm_agent.manager import manager_argv

    argv = manager_argv(manager, (
        f"Fetch the Notion data source {cfg.data_source} and reply with its title and "
        "nothing else. Do not create or modify anything, and do not use any other tool."))
    try:
        raw = run(" ".join(quote(a) for a in argv))
        result = json.loads(raw[raw.index("{"):])
    except (RemoteError, ValueError) as exc:
        return Check(name="task database", ok=False, detail=str(exc)[:140],
                     fix="check config/tasks.yaml and that the Notion MCP is authorised")
    title = str(result.get("result", "")).strip()
    ok = not result.get("is_error") and bool(title)
    return Check(name="task database", ok=ok,
                 detail=f"{cfg.data_source.split('://')[-1][:8]}… is {title[:60]!r}" if ok
                 else title[:140] or "the session returned nothing",
                 fix=None if ok else
                 "authorise the Notion MCP here, or fix data_source in config/tasks.yaml")


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
        # The main config `Include`s this one, so a missing node file breaks ssh just as
        # surely as a missing host block — one row covers both.
        (ssh_dir / "tillicum-node-config").write_text("Host tillicum-node\n")
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
    # A channel you turned OFF must not be demanded, and a channel you turned ON must be —
    # the second is the one that matters, because a hand-kept list gets it wrong silently
    # and the escalation never arrives.
    with tempfile.TemporaryDirectory() as tmp:
        envrc = Path(tmp) / ".envrc"
        envrc.write_text("x\n")
        envrc.chmod(0o600)
        smtp = {"SLURM_AGENT_SMTP_HOST": "smtp.x", "SLURM_AGENT_SMTP_USER": "me",
                "SLURM_AGENT_SMTP_PASSWORD": "pw"}

        def keys_row(channels, env):
            rows = healthcheck(cluster, ManagerConfig(), {}, FakeRunner(),
                               envrc=envrc, env=env, notify=NotifyConfig(channels=channels))
            return next(c for c in rows if c.name == "my keys")

        # Email only, and the webhook absent: nothing is missing.
        email_only = keys_row(["email"], smtp)
        assert email_only.ok, email_only.detail
        assert "SLURM_AGENT_SLACK_WEBHOOK" in email_only.detail
        assert "not needed" in email_only.detail

        # Turn Slack on without adding the key, and it fails — loudly, here, rather than
        # at the moment an agent needed a human.
        both = keys_row(["email", "slack"], smtp)
        assert both.ok is False
        assert "SLURM_AGENT_SLACK_WEBHOOK" in both.detail

        # The SMTP port has a default, so its absence is noted and never a failure.
        assert "defaulted" in email_only.detail
        assert "SLURM_AGENT_SMTP_PORT" in email_only.detail
        display(render([email_only, both]))


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
                             "ls-remote": "abc\trefs/heads/main\n", "remote get-url": "x\n",
                             "status --porcelain": "",
                             "cat": "export HF_TOKEN=real\n", "test -f": "yes"})
        # `local` is injected, so the suite never reaches the network: the git-auth rows
        # run HERE by default, and a test that silently called out to github would be
        # slow, flaky offline, and dependent on whoever is logged in.
        rows = healthcheck(cluster, manager, hungry, runner, envrc=envrc, env=good_env,
                           local=FakeRunner({"ls-remote": "abc\trefs/heads/main\n"}))
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


# %%
if test():
    # `poe init` produces ONE report. What it created is folded into the row about that
    # thing, and a creation that failed says nothing at all — the check that follows is
    # already about to say the login node is unreachable, in one line instead of ssh's four.
    with tempfile.TemporaryDirectory() as tmp:
        envrc, ssh_dir = Path(tmp) / ".envrc", Path(tmp) / "ssh"

        class _NoLogin(FakeRunner):
            def __call__(self, command, stdin=None):
                self.commands.append(command)
                raise RemoteError(command, "ssh_askpass: exec(...): No such file or "
                                           "directory\nPermission denied (gssapi-keyex).")

        runner = _NoLogin()
        notes = init(ClusterConfig(login_host="h"), manager, {}, runner,
                     envrc=envrc, ssh_dir=ssh_dir)
        # It tried, and said nothing about failing.
        assert runner.asked("mkdir -p")
        assert not any(name == "run root" for _, name in notes)
        assert "just created" in notes[(LAPTOP, ".envrc")]
        # A note must never land on a row of the same name somewhere else: three machines
        # each have an `.envrc`, and only one of them was touched here.
        assert all(where == LAPTOP for where, _ in notes)

        rows = healthcheck(ClusterConfig(login_host="h"), manager, {}, runner, envrc=envrc,
                           env={}, ssh_dir=ssh_dir, created=notes)
        by_name = {c.name: c for c in rows}
        # The note rides along inside the row it is about, not in a section above it.
        assert "just created" in by_name[".envrc"].detail
        assert "appended" in by_name["ssh config"].detail
        # One voice on the unreachable login node, and it is not ssh's.
        assert by_name["reachable"].detail == "not authenticated"
        assert by_name["run root"].ok is None
        assert "ssh_askpass" not in render(rows)
        display(render(rows))


# %%
if test():
    # The real thing ssh printed when a login node had not been authenticated to: four
    # lines about a missing X11 binary that is neither the cause nor fixable, and the
    # reason last. Reporting all of it buries the one sentence that matters.
    noisy = ("'id -un' failed: ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such "
             "file or directory\nssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such "
             "file or directory\nssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such "
             "file or directory\ndeanlcs@tillicum.hyak.uw.edu: Permission denied "
             "(gssapi-keyex,gssapi-with-mic,keyboard-interactive).")
    assert ssh_reason(noisy) == "not authenticated"
    assert ssh_reason("ssh: Could not resolve hostname x") .startswith("host not found")
    assert ssh_reason("timed out after 60s") == "timed out"
    # Unrecognised failures keep ssh's own last word rather than an invented summary —
    # but never the askpass repetition, which is never the cause.
    odd = "ssh_askpass: exec(...): No such file\nkex_exchange_identification: bad banner"
    assert ssh_reason(odd) == "kex_exchange_identification: bad banner"


# %%
if test():
    # The task database is proved on the laptop, in the full tier, because `poe task-new`
    # runs there — and a launch with no task id is not allowed, so this failing later means
    # an allocation brought up for work that cannot legally start.
    from slurm_agent.tasks import TaskConfig as _TC

    cfg_t = _TC(data_source="collection://abc")
    good = FakeRunner({"claude": '{"result": "Tasks", "is_error": false}'})
    row = _task_database(good, manager, cfg_t)
    assert row.ok and "Tasks" in row.detail
    # Bounded: the session that reads Notion may reach Notion and nothing else.
    assert "--strict-mcp-config" in good.commands[0]
    assert "Do not create or modify anything" in good.commands[0]
    # The preamble rides along, so this proves the path a real ask takes.
    assert "slurm-orchestration/SKILL.md" in good.commands[0]

    bad = FakeRunner({"claude": '{"result": "not authorised", "is_error": true}'})
    assert _task_database(bad, manager, cfg_t).ok is False
    display(render([row]))


# %%
if test():
    # Git auth is asked of BOTH machines. Read access is all `ls-remote` can prove, and the
    # row must not imply more — a public repo answers it with no credential at all.
    ok_rows = _github_access(FakeRunner({"ls-remote": "a\trefs/heads/main\nb\trefs/heads/x\n"}),
                             ["DeanLight/slurm-agent"], LAPTOP)
    assert len(ok_rows) == 1 and ok_rows[0].ok
    assert "2 branches" in ok_rows[0].detail and "read only" in ok_rows[0].detail

    denied = _github_access(FakeRunner({"ls-remote": "remote: Repository not found.\n"}),
                            ["DeanLight/private"], LAPTOP)
    assert denied[0].ok is False and "gh auth login" in denied[0].fix
    display(render(ok_rows + denied))


# %%
if test():
    # `--full` adds the question that actually matters for an agent: can it PUSH? A run
    # that cannot push has spent its GPU-hour for nothing, and read access does not imply
    # write. The probe is a dry run, so it authorises and then writes nothing.
    pushy = FakeRunner({"ls-remote": "a\trefs/heads/main\n",
                        "push --dry-run": "To github.com\n * [new branch] HEAD -> probe\n"})
    rows = _github_access(pushy, ["DeanLight/slurm-agent"], LAPTOP, push=True)
    assert [r.ok for r in rows] == [True, True]
    assert "nothing was pushed" in rows[1].detail
    assert pushy.asked("--dry-run")

    refused = FakeRunner({"ls-remote": "a\trefs/heads/main\n",
                          "push --dry-run": "remote: Permission to x denied to y.\n"})
    rows = _github_access(refused, ["DeanLight/slurm-agent"], LAPTOP, push=True)
    assert rows[1].ok is False and "denied" in rows[1].detail


# %%
if test():
    # A workdir is created by the first LAUNCH, not by setup. "Not cloned yet" is the
    # normal state of a fresh clone and must not read as a fault — while a clone pointing
    # at the wrong repo, or a dirty one, must, because both stop or spoil the next launch.
    agent_cfg = AgentConfig(repo="DeanLight/slurm-agent", ref="main", workdir="~/work/x",
                            log_dir="e", max_budget_usd=1)
    fresh = FakeRunner({"test -d": "absent\n", "stat -c": "none\n"})
    rows = {c.name: c for c in _remote_envrc(fresh, cluster, {"k": agent_cfg})}
    assert rows["clone"].ok and "not cloned yet" in rows["clone"].detail

    wrong = FakeRunner({"test -d": "repo\n", "remote get-url": "https://github.com/other/y\n",
                        "status --porcelain": " M a.py\n M b.py\n", "stat -c": "none\n"})
    rows = {c.name: c for c in _remote_envrc(wrong, cluster, {"k": agent_cfg})}
    assert rows["clone"].ok is False and "agents/k.yaml says" in rows["clone"].detail
    # A dirty tree is reported HERE, not discovered when the launch refuses it.
    assert rows["worktree"].ok is False and "2 uncommitted" in rows["worktree"].detail
    display(render(list(rows.values())))
