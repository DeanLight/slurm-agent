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
# # The usage monitor
#
# A scheduled job that polls spend, keeps a ledger, and **speaks only when it has
# something to say**. Silence has to mean "nothing changed", so a message always means
# something did.
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

from slurm_agent.config import MonitorConfig
from slurm_agent.remote import Runner

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
# ## The ledger and the digest
#
# The comparison is against the last row that was actually **sent**, not the last one
# observed — otherwise three unsent polls in a row would hide a change that happened
# across them.

# %%
def append(ledger_path: Path | str, row: dict) -> None:
    """Append one observation. JSONL: append-only, no database, readable with `tail`."""
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def rows(ledger_path: Path | str) -> list[dict]:
    path = Path(ledger_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def last_sent(ledger_path: Path | str) -> dict | None:
    return next((r for r in reversed(rows(ledger_path)) if r.get("sent")), None)


def digest(current: dict, ledger_path: Path | str, cfg: MonitorConfig,
           batch: list[dict] | None = None) -> str | None:
    """The message to send, or None when there is nothing to say."""
    previous = last_sent(ledger_path)
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
    import tempfile

    cfg = MonitorConfig()
    now = {"safedesign": {"used_usd": 412.30, "limit_usd": 900.0,
                          "used_gpu_hours": 458.11, "limit_gpu_hours": 1000.0}}

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        body = digest(now, ledger, cfg)
        assert body and "412.30" in body           # first poll always has news
        display(body)

        append(ledger, {"observed_at": "2026-08-22", "usage": now, "sent": True})
        # Unchanged spend says nothing at all.
        assert digest(now, ledger, cfg) is None


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        append(ledger, {"observed_at": "2026-08-22", "usage": now, "sent": True})
        moved = {"safedesign": dict(now["safedesign"], used_usd=498.30)}
        # Three unsent polls in between must not hide the change.
        for day in ("23", "24", "25"):
            append(ledger, {"observed_at": f"2026-08-{day}", "usage": moved, "sent": False})
        body = digest(moved, ledger, cfg)
        assert body and "+86.00" in body and "since 2026-08-22" in body
        display(body)


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        hot = {"safedesign": dict(now["safedesign"], used_usd=800.0)}
        append(ledger, {"observed_at": "x", "usage": hot, "sent": True})
        # Flat spend, but over the budget threshold: still worth a message.
        body = digest(hot, ledger, cfg)
        assert body and "ALERT" in body and "89%" in body   # 800 of 900

        # And batch news alone is enough to send.
        flat = digest(hot, ledger, cfg, batch=[{"session_id": "9c03aaaa", "task": "T",
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


def cron_status(ledger_path: Path | str, current: str | None = None) -> str:
    """Installed or not, when it last ran, when a digest was last sent."""
    text = current if current is not None else _crontab(["-l"])
    installed = MARK_START in text
    observed = rows(ledger_path)
    sent = last_sent(ledger_path)
    return (f"{'installed' if installed else 'NOT installed'} · "
            f"last ran {observed[-1]['observed_at'] if observed else 'never'} · "
            f"last digest sent {sent['observed_at'] if sent else 'never'}")


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

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        append(ledger, {"observed_at": "2026-09-01", "usage": {}, "sent": False})
        report = cron_status(ledger, current=once)
        assert "installed" in report and "last ran 2026-09-01" in report
        assert "last digest sent never" in report
        display(report)


# %%
def monitor_run(run: Runner, cfg: MonitorConfig, ledger_path: Path | str, *,
                dry_run: bool = False, batch: list[dict] | None = None,
                send=None) -> str:
    """The scheduled entry point: poll, append to the ledger, send only if there is news."""
    current = usage(run)
    observed_at = time.strftime("%Y-%m-%d")
    body = digest(current, ledger_path, cfg, batch)
    if body is None:
        previous = last_sent(ledger_path) or {}
        append(ledger_path, {"observed_at": observed_at, "usage": current, "sent": False})
        return (f"spend unchanged since {previous.get('observed_at', 'the last digest')} "
                "— nothing to send")
    if dry_run:
        return body
    if send is not None:
        send("[slurm-agent] usage digest", body)
    append(ledger_path, {"observed_at": observed_at, "usage": current, "sent": True})
    return body


# %%
if test():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        runner = FakeRunner({"hyakusage": sample})
        sent_msgs: list[tuple] = []

        first = monitor_run(runner, cfg, ledger, send=lambda s, b: sent_msgs.append((s, b)))
        assert "412.30" in first and len(sent_msgs) == 1

        # Second poll, unchanged: records the observation, sends nothing.
        second = monitor_run(runner, cfg, ledger, send=lambda s, b: sent_msgs.append((s, b)))
        assert "nothing to send" in second
        assert len(sent_msgs) == 1
        assert [r["sent"] for r in rows(ledger)] == [True, False]
        display(second)

        # --dry-run never sends and never marks a row sent.
        moved = FakeRunner({"hyakusage": sample.replace("412.30", "498.30")})
        preview = monitor_run(moved, cfg, ledger, dry_run=True,
                              send=lambda s, b: sent_msgs.append((s, b)))
        assert "498.30" in preview and len(sent_msgs) == 1
