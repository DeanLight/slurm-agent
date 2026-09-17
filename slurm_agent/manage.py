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
# # Opening the manager
#
# `poe manage "Pick up T2-132 on Tillicum"` is the entry point for everything the manager
# does: spinning work up, asking how it is going, reporting what it cost.
#
# It **configures nothing**. `CLAUDE.md`, the skill, `.mcp.json` and `.claude/settings.json`
# are what make a session in this directory the manager, and a bare `claude` here is still
# exactly that. What this adds is only what lives on a command line and cannot be checked
# in: the Remote Control name, resume, and the prompt you would otherwise type first.
#
# ## Why this is Python and not a `poe` shell task
#
# A `poe` **shell** task cannot open an interactive program, and the failure is silent.
# Poe runs the interpreter with the script body on **stdin** (`executor.execute(...,
# input=content.encode())`), so the child's stdin is a pipe — the script itself. `exec
# claude` inherits that pipe, Claude Code finds no terminal to read from, and you get
# something that is not a conversation instead of an error.
#
# A `poe` **cmd** task inherits the real terminal on both ends. So the shape that works is
# the shape this repo already uses everywhere else: one `poe` task wrapping one
# `slurm-agent` command, which builds an argv and `execvp`s it. `exec` keeps the
# terminal, and the interesting part — which flags, in which order — becomes a pure
# function with tests instead of quoting inside a heredoc.

# %%
import os
from pathlib import Path

import structlog
from IPython.display import display
from juplit import test

log = structlog.get_logger(__name__)

# The one call that leaves this process, named so a test can stand in front of it — the
# same reason `remote.py` has runner factories. A smoke run that really called `execvp`
# would replace pytest with a Claude session.
EXEC = os.execvp

SESSION_ENV = "SLURM_AGENT_MANAGER_SESSION"
FRESH_ENV = "SLURM_AGENT_MANAGER_FRESH"
DEFAULT_SESSION = "Tillicum manager"


# %% [markdown]
# ## Has this directory a conversation to come back to?
#
# Resume is not implied by `--remote-control`. That flag starts an interactive session with
# Remote Control on and names the remote link; a name is not a conversation identity, so
# typing the same one tomorrow gives you a second, empty conversation beside yesterday's.
# `--continue` is what resumes, and it fails outright when there is nothing to continue —
# hence looking first rather than passing the flag and hoping.
#
# Resuming is what makes "how are my runs going?" answerable: it reaches the session that
# launched them. The transcript directory is Claude Code's own, addressed by its slugged
# path convention. If that guess ever misses, the fallback is a fresh session — a lost
# resume, never a failed launch.

# %%
def transcript_dir(cwd: Path | str, home: Path | str | None = None) -> Path:
    """Where Claude Code keeps this directory's conversations."""
    home = Path(home) if home is not None else Path.home()
    return home / ".claude" / "projects" / str(Path(cwd)).replace("/", "-")


def has_conversation(cwd: Path | str, home: Path | str | None = None) -> bool:
    directory = transcript_dir(cwd, home)
    try:
        return any(directory.glob("*.jsonl"))
    except OSError:
        return False


# %%
if test():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        where = transcript_dir("/home/deanlcs/src/slurm-agent", home)
        assert where.name == "-home-deanlcs-src-slurm-agent"

        # Nothing here yet is the normal state of a fresh clone, and must not read as an
        # error — it is the difference between --continue and a first conversation.
        assert has_conversation("/home/deanlcs/src/slurm-agent", home) is False
        where.mkdir(parents=True)
        (where / "a1b2.jsonl").write_text("{}\n")
        assert has_conversation("/home/deanlcs/src/slurm-agent", home) is True
        display(str(where))


# %% [markdown]
# ## The argv
#
# `-p` is the same manager printing its reply and exiting, for a shell that captures it —
# `IDS=$(poe manage -p '…')`. Remote Control is interactive-only, so `-p` never asks for
# it.

# %%
def manager_argv(prompt: str = "", *, headless: bool = False, resume: bool = False,
                 session: str = DEFAULT_SESSION) -> list[str]:
    """The `claude` command line for a manager session. Pure, so it can be asserted on."""
    argv = ["claude"]
    if resume:
        argv.append("--continue")
    if headless:
        argv.append("-p")
    else:
        # The name is ALWAYS passed. `--remote-control` takes an optional value, so
        # leaving it off would let the prompt be swallowed as the session name.
        argv += ["--remote-control", session]
    if prompt:
        argv.append(prompt)
    return argv


# %%
if test():
    assert manager_argv() == ["claude", "--remote-control", DEFAULT_SESSION]
    assert manager_argv("do the thing", resume=True) == [
        "claude", "--continue", "--remote-control", DEFAULT_SESSION, "do the thing"]

    # Headless never asks for Remote Control, which is interactive-only — and stdout stays
    # exactly the reply, because that is what `IDS=$( )` captures.
    head = manager_argv("two tasks please", headless=True, resume=True)
    assert head == ["claude", "--continue", "-p", "two tasks please"]
    assert "--remote-control" not in head

    # A prompt is one argv element however it is spelled: no quoting, no word splitting,
    # no shell in between.
    tricky = manager_argv("Pick up T2-132; it's $URGENT & \"big\"")
    assert tricky[-1] == "Pick up T2-132; it's $URGENT & \"big\""
    display(manager_argv("Pick up T2-132 on Tillicum", resume=True))


# %% [markdown]
# ## Handing over the terminal
#
# `execvp` replaces this process, so the file descriptors it was given — the real terminal,
# when `poe` ran it as a `cmd` task — become Claude Code's. Nothing is left between you and
# the session.

# %%
def manage(prompt: str = "", *, headless: bool = False, env: dict[str, str] | None = None,
           cwd: Path | str | None = None, exec_=None) -> list[str]:
    """Decide the argv, say why on stderr, and become `claude`."""
    env = os.environ if env is None else env
    cwd = Path.cwd() if cwd is None else cwd

    if env.get(FRESH_ENV):
        resume, why = False, f"starting a fresh conversation ({FRESH_ENV} is set)"
    elif has_conversation(cwd):
        resume, why = True, "resuming the most recent conversation in this directory"
    else:
        resume, why = False, "no previous conversation here — starting a fresh one"

    argv = manager_argv(prompt, headless=headless, resume=resume,
                        session=env.get(SESSION_ENV) or DEFAULT_SESSION)
    # stderr, always: under `-p` stdout is the reply and nothing else.
    log.info("manage.open", why=why, argv=argv)
    (exec_ or EXEC)(argv[0], argv)
    return argv


# %%
if test():
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        # FRESH wins over an existing conversation, which is the whole point of the switch.
        home = Path(tmp)
        where = transcript_dir(tmp, home)
        where.mkdir(parents=True)
        (where / "a.jsonl").write_text("{}\n")

        real_home, os.environ["HOME"] = os.environ.get("HOME"), str(home)
        try:
            assert "--continue" in manage(cwd=tmp, env={},
                                          exec_=lambda f, a: calls.append(a))
            assert "--continue" not in manage(cwd=tmp, env={FRESH_ENV: "1"},
                                              exec_=lambda f, a: calls.append(a))
            # The session name is an env override, never a committed value.
            named = manage(cwd=tmp, env={SESSION_ENV: "klone"},
                           exec_=lambda f, a: calls.append(a))
            assert "klone" in named
        finally:
            if real_home is not None:
                os.environ["HOME"] = real_home

    # `execvp` was called with the program as both the file and argv[0], every time.
    assert len(calls) == 3 and all(a[0] == "claude" for a in calls)
    display(calls[-1])
