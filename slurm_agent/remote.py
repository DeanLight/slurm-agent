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
# # The remote seam
#
# One function reaches the cluster, and everything else in this repo is built on it.
#
# It shells out to `ssh` rather than using a library, because the `ControlMaster` socket in
# `~/.ssh/config` is what makes UW's 2FA a once-a-day event — a library that opens its own
# connections would re-authenticate and buy nothing.
#
# `Runner` is also the single seam the whole test suite injects at: every function that
# touches the cluster takes one, so tests pass a dict-backed fake and no mocking library is
# needed anywhere in this repo.

# %%
import shlex
import subprocess
from collections.abc import Callable

import structlog
from IPython.display import display
from juplit import test

log = structlog.get_logger(__name__)

Runner = Callable[[str], str]
"""Run one shell command on the login node and return its stdout."""


# %%
class RemoteError(RuntimeError):
    """A command on the login node failed, timed out, or returned something unreadable."""

    def __init__(self, command: str, detail: str, returncode: int | None = None):
        self.command = command
        self.detail = detail
        self.returncode = returncode
        super().__init__(f"{command!r} failed: {detail}")


# %%
def ssh_runner(host: str, *, timeout: int = 60) -> Runner:
    """Build a `Runner` that executes commands on `host` over the shared ssh connection."""

    def run(command: str, stdin: str | None = None) -> str:
        argv = ["ssh", host, command]
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, input=stdin
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(command, f"timed out after {timeout}s") from exc
        if done.returncode != 0:
            raise RemoteError(command, done.stderr.strip(), done.returncode)
        return done.stdout

    return run


# %%
if test():
    # Assert the argv we would run, without needing a cluster: the closure is the unit
    # under test here, and `ssh` itself is not ours to verify.
    seen: list[list[str]] = []

    class _Done:
        returncode, stdout, stderr = 0, "g004\n", ""

    def _fake(argv, **kwargs):
        seen.append(argv)
        return _Done()

    real_run, subprocess.run = subprocess.run, _fake
    try:
        assert ssh_runner("tillicum-login")("squeue --me") == "g004\n"
    finally:
        subprocess.run = real_run

    assert seen == [["ssh", "tillicum-login", "squeue --me"]]
    display(seen[0])


# %%
if test():
    class _Failed:
        returncode, stdout, stderr = 1, "", "salloc: error: QOSMaxGRESPerUser\n"

    real_run, subprocess.run = subprocess.run, lambda argv, **kw: _Failed()
    try:
        ssh_runner("tillicum-login")("salloc --gpus=99")
        raise AssertionError("a non-zero exit should have raised")
    except RemoteError as exc:
        # stderr is carried, never swallowed: it is the only explanation the user gets.
        assert "QOSMaxGRESPerUser" in str(exc)
        assert exc.returncode == 1
        display(str(exc))
    finally:
        subprocess.run = real_run


# %%
def quote(value: str) -> str:
    """Shell-quote one value for interpolation into a remote command."""
    return shlex.quote(value)


# %%
def remote_path(path: str | object) -> str:
    """Render a path for interpolation into a remote command, expanding `~` via `$HOME`.

    `quote` alone is wrong for a home-relative path: it quotes the tilde, so the remote
    shell receives it literally and never expands it. Quoting only the tail keeps `$HOME`
    expandable while still making the rest injection-safe.
    """
    path = str(path)
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + quote(path[2:])
    return quote(path)


# %%
if test():
    assert remote_path("~/.slurm-agent/runs") == '"$HOME"/.slurm-agent/runs'
    assert remote_path("~") == '"$HOME"'
    assert remote_path("/scratch/runs") == "/scratch/runs"
    # The tail is still quoted, so a hostile path cannot break out of the command.
    assert remote_path("~/a b") == '"$HOME"/\'a b\''
    assert remote_path("~/$(rm -rf /)") == '"$HOME"/\'$(rm -rf /)\''
    display([remote_path(p) for p in ("~/.slurm-agent/runs", "~/a b", "/scratch/x")])


# %%
if test():
    assert quote("plain") == "plain"
    assert quote("a b") == "'a b'"
    # `~` is NOT in shlex's safe set, so a home-relative path comes back quoted and the
    # remote shell will not expand it. Remote paths are resolved with $HOME, never `~`.
    assert quote("~/work/repo") == "'~/work/repo'"
    assert quote("$(rm -rf /)") == "'$(rm -rf /)'"
    display([quote(v) for v in ("plain", "a b", "~/work/repo", "$(rm -rf /)")])
