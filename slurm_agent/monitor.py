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
# # The usage monitor
#
# A scheduled job that polls spend, keeps a ledger on the cluster, and **records only
# when it has something to say**. Silence has to mean "nothing changed", so an entry
# always means something did.
#
# It writes; it does not deliver. The manager reads the ledger with `poe spend` and tells
# you, which is why there is no sender here: the manager is the channel, and it is the
# one already in a conversation with you.
#
# It **reports and never acts**. Kill authority belongs to the manager alone; this is a
# cron entry nobody is watching, and a cron entry that can spend money is the thing the
# spec's out-of-scope bullet was protecting against.

# %%
import json
import re
import subprocess
import time
from pathlib import Path

import structlog
from IPython.display import display
from juplit import test

from slurm_agent.config import ClusterConfig, MonitorConfig
from slurm_agent.remote import Runner, remote_path

log = structlog.get_logger(__name__)

MARK_START = "# >>> slurm-agent monitor >>>"
MARK_END = "# <<< slurm-agent monitor <<<"


# %%
def usage(run: Runner) -> dict[str, dict[str, float]]:
    """Run `hyakusage` on the login node and parse it into per-account totals.

    Written against a captured sample committed as a fixture — the format is the one thing
    here nobody could verify from a real cluster at design time, so the parser is
    deliberately loose about columns and strict about what it found.
    """
    raw = run("hyakusage")
    accounts: dict[str, dict[str, float]] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 5 or not re.fullmatch(r"[a-z][\w-]*", fields[0]):
            continue
        try:
            used_h, limit_h, used_usd, limit_usd = (float(f) for f in fields[1:5])
        except ValueError:
            continue
        accounts[fields[0]] = {"used_gpu_hours": used_h, "limit_gpu_hours": limit_h,
                               "used_usd": used_usd, "limit_usd": limit_usd}
    if not accounts:
        head = "\n".join(raw.splitlines()[:6])
        raise ValueError(f"could not parse hyakusage output; it began:\n{head}")
    return accounts


# %%
if test():
    from tests.conftest import FakeRunner

    sample = (Path(__file__).resolve().parent.parent / "tests" / "fixtures"
              / "hyakusage.txt").read_text()
    parsed = usage(FakeRunner({"hyakusage": sample}))
    assert set(parsed) == {"safedesign", "stf"}
    assert parsed["safedesign"]["used_usd"] == 412.30
    assert parsed["stf"]["limit_gpu_hours"] == 250.0
    display(parsed)

    try:
        usage(FakeRunner({"hyakusage": "command not found: hyakusage"}))
        raise AssertionError("unparseable output should have raised")
    except ValueError as exc:
        # The message carries the raw head, so the fixture can be updated from it alone.
        assert "command not found" in str(exc)
        display(str(exc))


# %% [markdown]
# ## The ledger, on the cluster
#
# The digest is written where every other fact this repo reports lives: the shared
# filesystem, under the run root. That is the one rule, and it is not ceremony here — the
# cron entry and the manager are different processes waking on different schedules, and a
# laptop-local ledger would mean a reinstall loses the spend history and two sessions
# disagree about it.
#
# The poll already ssh's in to run `hyakusage`, so writing the answer back costs one more
# command and no new dependency. When the tunnel is down nothing is polled and nothing is
# written, and the gap in the file is an honest record of that rather than a lie about
# spend not moving.
#
# The comparison is against the last row that was actually a **digest**, not the last one
# observed — otherwise three quiet polls in a row would hide a change that happened
# across them.

# %%
def ledger_path(cluster: ClusterConfig) -> str:
    """Where the digest lives: beside the runs, on the cluster."""
    return f"{str(cluster.run_root).rstrip('/')}/usage.jsonl"


def append(run: Runner, path: str, row: dict) -> None:
    """Append one observation. JSONL: append-only, no database, readable with `tail`."""
    line = json.dumps(row, sort_keys=True)
    parent = path.rsplit("/", 1)[0]
    run(f"mkdir -p {remote_path(parent)} && cat >> {remote_path(path)} "
        f"<<'SLURM_AGENT_EOF'\n{line}\nSLURM_AGENT_EOF")


def rows(run: Runner, path: str) -> list[dict]:
    """Every observation, oldest first. A missing ledger is an empty one, not an error."""
    raw = run(f"cat {remote_path(path)} 2>/dev/null || true")
    out: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            # A half-written line from an append that died mid-flight loses one reading.
            # Refusing to read the other four hundred would be the worse failure.
            continue
    return out


def last_digest(history: list[dict]) -> dict | None:
    return next((r for r in reversed(history) if r.get("digest")), None)


# %%
if test():
    from tests.conftest import FakeRunner as _FR

    writer = _FR({})
    append(writer, "~/.slurm-agent/usage.jsonl", {"usage": {}, "digest": True})
    command = writer.commands[0]
    # `~` never crosses the ssh boundary: shlex.quote would quote it and the remote shell
    # would take it literally, writing a directory named `~` in the home directory.
    assert "~" not in command and '"$HOME"/.slurm-agent' in command
    assert command.startswith("mkdir -p ") and "cat >> " in command
    display(command)

    # A ledger that is not there yet reads as empty, because the first poll is normal.
    assert rows(_FR({}), "~/x.jsonl") == []
    ledger = _FR({"cat": '{"observed_at": "a", "digest": false}\n'
                         '{"observed_at": "b", "digest": true}\n'
                         '{ this line was half written\n'})
    history = rows(ledger, "~/x.jsonl")
    assert len(history) == 2 and last_digest(history)["observed_at"] == "b"


# %%
def digest(current: dict, previous: dict | None, cfg: MonitorConfig,
           batch: list[dict] | None = None) -> str | None:
    """The entry to record, or None when there is nothing to say.

    Pure: it is handed the last digest row rather than going and finding it, so the
    interesting cases below are three lines of dicts instead of a temporary directory.
    """
    prior = (previous or {}).get("usage", {})
    changed = prior != current

    alerts = []
    for name, totals in current.items():
        limit = totals.get("limit_usd") or 0
        if limit and (totals["used_usd"] / limit) * 100 >= cfg.budget_used_pct:
            alerts.append(f"{name}: {totals['used_usd']:.2f} of {limit:.2f} "
                          f"({totals['used_usd'] / limit:.0%} of budget)")

    if not changed and not alerts and not batch and cfg.only_if_changed:
        return None

    lines = []
    for name, totals in sorted(current.items()):
        before = prior.get(name, {}).get("used_usd")
        delta = f" (+{totals['used_usd'] - before:.2f})" if before is not None else ""
        lines.append(f"{name:<14} ${totals['used_usd']:.2f}{delta} of "
                     f"${totals.get('limit_usd', 0):.2f}")
    if alerts:
        lines.append("")
        lines.extend(f"ALERT {a}" for a in alerts)
    if batch:
        lines.append("")
        lines.append("batch jobs since the last digest:")
        lines.extend(f"  {b.get('session_id', '?')[:8]}  {b.get('task', '?'):<10} "
                     f"{b.get('job_state', '?')}" for b in batch)
    since = (previous or {}).get("observed_at", "the first poll")
    lines.append("")
    lines.append(f"since {since}")
    return "\n".join(lines)


# %%
if test():
    cfg = MonitorConfig()
    now = {"safedesign": {"used_usd": 412.30, "limit_usd": 900.0,
                          "used_gpu_hours": 458.11, "limit_gpu_hours": 1000.0}}

    body = digest(now, None, cfg)
    assert body and "412.30" in body           # the first poll always has news
    display(body)

    # Unchanged spend says nothing at all.
    assert digest(now, {"observed_at": "2026-08-22", "usage": now, "digest": True},
                  cfg) is None


# %%
if test():
    # Three quiet polls in between must not hide the change: the baseline is the last
    # DIGEST row, which is why `last_digest` skips the others rather than taking the tail.
    moved = {"safedesign": dict(now["safedesign"], used_usd=498.30)}
    history = [{"observed_at": "2026-08-22", "usage": now, "digest": True}]
    history += [{"observed_at": f"2026-08-{d}", "usage": moved, "digest": False}
                for d in ("23", "24", "25")]
    body = digest(moved, last_digest(history), cfg)
    assert body and "+86.00" in body and "since 2026-08-22" in body
    display(body)


# %%
if test():
    hot = {"safedesign": dict(now["safedesign"], used_usd=800.0)}
    previous = {"observed_at": "x", "usage": hot, "digest": True}
    # Flat spend, but over the budget threshold: still worth recording.
    body = digest(hot, previous, cfg)
    assert body and "ALERT" in body and "89%" in body   # 800 of 900

    # And batch news alone is enough.
    flat = digest(hot, previous, cfg, batch=[{"session_id": "9c03aaaa", "task": "T",
                                              "job_state": "TIMEOUT"}])
    assert flat and "TIMEOUT" in flat
    display(flat)


# %% [markdown]
# ## The schedule
#
# One marker-delimited block in the user's crontab. The markers are why this needs no
# dependency and never touches a line it did not write, and why install and remove are
# both idempotent.

# %%
def _crontab(args: list[str], stdin: str | None = None) -> str:
    try:
        done = subprocess.run(["crontab", *args], capture_output=True, text=True,
                              input=stdin)
    except FileNotFoundError:
        raise RuntimeError(
            "no `crontab` on this machine — install the schedule with launchd (macOS) or "
            "a systemd timer, then `poe monitor-status` will still report it"
        ) from None
    if done.returncode != 0 and "no crontab" not in done.stderr:
        raise RuntimeError(f"crontab failed: {done.stderr.strip()}")
    return done.stdout


def _strip_block(text: str) -> str:
    """Everything except our marked block, so other entries survive byte-for-byte."""
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == MARK_START:
            skipping = True
        elif line.strip() == MARK_END:
            skipping = False
        elif not skipping:
            out.append(line)
    return "\n".join(out)


def cron_write(block: str | None, *, current: str | None = None,
               apply: bool = True) -> str:
    """Install (block=text) or remove (block=None) our one marked crontab entry."""
    existing = _strip_block(current if current is not None else _crontab(["-l"]))
    lines = [ln for ln in existing.splitlines() if ln.strip()]
    if block:
        lines += [MARK_START, block, MARK_END]
    result = "\n".join(lines) + ("\n" if lines else "")
    if apply:
        _crontab(["-"], stdin=result)
    return result


def cron_line(cfg: MonitorConfig, repo_root: Path | str) -> str:
    return (f"0 9 */{cfg.every_days} * * cd {repo_root} && uv run slurm-agent monitor-run")


def cron_status(run: Runner, path: str, current: str | None = None) -> str:
    """Installed or not, when it last ran, when it last had something to say."""
    text = current if current is not None else _crontab(["-l"])
    installed = MARK_START in text
    history = rows(run, path)
    recorded = last_digest(history)
    return (f"{'installed' if installed else 'NOT installed'} · "
            f"last ran {history[-1]['observed_at'] if history else 'never'} · "
            f"last digest {recorded['observed_at'] if recorded else 'never'}")


# %%
if test():
    other = "0 6 * * * /usr/bin/backup\n"
    line = cron_line(MonitorConfig(), "/repo")
    once = cron_write(line, current=other, apply=False)
    assert once.count(MARK_START) == 1
    assert "/usr/bin/backup" in once

    # Installing twice leaves exactly one block, not two.
    twice = cron_write(line, current=once, apply=False)
    assert twice.count(MARK_START) == 1
    assert twice == once
    display(once)


# %%
if test():
    # Removing restores the other entries untouched.
    removed = cron_write(None, current=once, apply=False)
    assert MARK_START not in removed
    assert removed.strip() == other.strip()

    quiet = _FR({"cat": '{"observed_at": "2026-09-01", "usage": {}, "digest": false}\n'})
    report = cron_status(quiet, "~/x.jsonl", current=once)
    assert "installed" in report and "last ran 2026-09-01" in report
    assert "last digest never" in report
    display(report)


# %%
def monitor_run(run: Runner, cfg: MonitorConfig, path: str, *,
                dry_run: bool = False, batch: list[dict] | None = None) -> str:
    """The scheduled entry point: poll, append to the ledger, record only if there is news.

    Every poll is written down, news or not, because "we looked and nothing had moved" and
    "nobody looked" are different facts and the manager has to be able to tell them apart.
    Only a row with news carries `digest: True`, and that is the baseline the next poll
    compares against.
    """
    current = usage(run)
    observed_at = time.strftime("%Y-%m-%d %H:%M")
    previous = last_digest(rows(run, path))
    body = digest(current, previous, cfg, batch)
    if body is None:
        append(run, path, {"observed_at": observed_at, "usage": current, "digest": False})
        return (f"spend unchanged since {previous.get('observed_at', 'the last digest')} "
                "— recorded the reading, nothing to report")
    if dry_run:
        return body
    # The rendered body is stored, not just the numbers: `spend` shows the manager exactly
    # what this poll saw, rather than a re-derivation that could drift from it.
    append(run, path, {"observed_at": observed_at, "usage": current, "digest": True,
                       "body": body})
    return body


# %%
if test():
    class Ledger:
        """A FakeRunner that actually accumulates what was appended to it."""

        def __init__(self, hyakusage: str):
            self.hyakusage, self.lines = hyakusage, []

        def __call__(self, command, stdin=None):
            if command.startswith("hyakusage"):
                return self.hyakusage
            if "cat >> " in command:
                _, _, rest = command.partition("<<'SLURM_AGENT_EOF'\n")
                self.lines.append(rest.split("\nSLURM_AGENT_EOF")[0])
                return ""
            return "\n".join(self.lines)

    ledger = Ledger(sample)
    first = monitor_run(ledger, cfg, "~/.slurm-agent/usage.jsonl")
    assert "412.30" in first
    assert json.loads(ledger.lines[0])["digest"] is True

    # Second poll, unchanged: records the reading, reports nothing.
    second = monitor_run(ledger, cfg, "~/.slurm-agent/usage.jsonl")
    assert "nothing to report" in second
    assert [json.loads(r)["digest"] for r in ledger.lines] == [True, False]
    display(second)

    # --dry-run never writes and never marks a row a digest.
    ledger.hyakusage = sample.replace("412.30", "498.30")
    preview = monitor_run(ledger, cfg, "~/.slurm-agent/usage.jsonl", dry_run=True)
    assert "498.30" in preview and len(ledger.lines) == 2


# %% [markdown]
# ## What the manager reads
#
# The cron entry writes and the manager speaks. `spend` is the seam between them: it
# renders what the scheduled polls recorded, so "what has this cost so far" is answered
# from readings taken while nobody was looking rather than from one taken this second.

# %%
def spend(run: Runner, path: str, *, limit: int = 3) -> str:
    """The recorded digests, newest last — the manager's answer to "what has this cost"."""
    history = rows(run, path)
    if not history:
        return ("no readings recorded yet — `poe monitor-install` schedules them, or "
                "`poe monitor-run` takes one now")
    recorded = [r for r in history if r.get("digest")]
    out = [f"{len(history)} readings · {len(recorded)} with news · "
           f"last poll {history[-1].get('observed_at', '?')}"]
    for row in recorded[-limit:]:
        out += ["", f"── {row.get('observed_at', '?')}",
                row.get("body") or "(recorded before bodies were kept)"]
    if not recorded:
        out += ["", "spend has not moved since the readings began"]
    return "\n".join(out)


# %%
if test():
    assert "no readings recorded yet" in spend(_FR({}), "~/x.jsonl")

    # Polls with no news still count, and still date the last look — silence that cannot
    # say when it was last checked is indistinguishable from a cron entry that died.
    quiet_only = _FR({"cat": '{"observed_at": "2026-09-01 09:00", "digest": false}\n'})
    report = spend(quiet_only, "~/x.jsonl")
    assert "1 readings · 0 with news" in report and "has not moved" in report

    told = _FR({"cat": '{"observed_at": "2026-09-01 09:00", "digest": false}\n'
                       '{"observed_at": "2026-09-04 09:00", "digest": true, '
                       '"body": "safedesign  $498.30 (+86.00) of $900.00"}\n'})
    report = spend(told, "~/x.jsonl")
    assert "+86.00" in report and "2026-09-04" in report
    display(report)
