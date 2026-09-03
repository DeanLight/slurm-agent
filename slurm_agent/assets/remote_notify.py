#!/usr/bin/env python3
"""The agent's outbound shim, copied into each run root at launch.

The compute node has the credentials and no route back to the laptop, so this is how a
3am `needs_human` reaches a person. Standard library only — it runs under whatever python
the experiment repo has.

    remote_notify.py --from-status          # what the SessionEnd hook calls
    remote_notify.py --subject S --body B   # what the agent calls on needs_human

Records the channels that succeeded back into the status block, which is what the
supervisor reads as `announced`. A notify failure never corrupts that block: if nothing
can be written, it exits non-zero and leaves the block untouched.
"""

import json
import os
import smtplib
import sys
import tempfile
import urllib.request
from email.message import EmailMessage


def run_dir() -> str:
    return os.environ.get("SLURM_AGENT_RUN_DIR", ".")


def read_status() -> dict:
    try:
        with open(os.path.join(run_dir(), "status.json")) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def read_launch() -> dict:
    try:
        with open(os.path.join(run_dir(), "launch.json")) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def send_email(subject: str, body: str) -> None:
    host = os.environ["SLURM_AGENT_SMTP_HOST"]
    user = os.environ["SLURM_AGENT_SMTP_USER"]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = os.environ.get("SLURM_AGENT_NOTIFY_TO", user)
    message.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SLURM_AGENT_SMTP_PORT") or 587),
                      timeout=30) as server:
        server.starttls()
        server.login(user, os.environ["SLURM_AGENT_SMTP_PASSWORD"])
        server.send_message(message)


def send_slack(text: str) -> None:
    webhook = os.environ["SLURM_AGENT_SLACK_WEBHOOK"]
    request = urllib.request.Request(
        webhook, data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=30).close()


def record(channels: list[str]) -> None:
    """Write the delivered channels back into the status block, atomically."""
    block = read_status()
    if not block:
        return
    block["announced_on"] = channels
    target = os.path.join(run_dir(), "status.json")
    handle, tmp = tempfile.mkstemp(dir=run_dir(), suffix=".tmp")
    with os.fdopen(handle, "w") as out:
        json.dump(block, out, indent=1, sort_keys=True)
    os.replace(tmp, target)


def main(argv: list[str]) -> int:
    flags = {}
    key = None
    for item in argv:
        if item.startswith("--"):
            key = item[2:].replace("-", "_")
            flags[key] = ""
        elif key:
            flags[key] = (flags[key] + " " + item).strip()

    if "from_status" in flags:
        status, launch = read_status(), read_launch()
        state = status.get("state", "unknown")
        task = launch.get("task", "a run")
        subject = f"[slurm-agent] {task} {state}"
        body = (f"task:     {task}\nstate:    {state}\n"
                f"round:    {status.get('round', '—')}\n"
                f"cells:    {status.get('cells_done', '—')}\n"
                f"waiting:  {', '.join(status.get('waiting_on') or []) or '—'}\n"
                f"notebook: {launch.get('notebook', '—')}\n"
                f"run dir:  {run_dir()}\n")
    else:
        subject = flags.get("subject") or "[slurm-agent] message"
        body = flags.get("body") or ""

    delivered, failed = [], []
    for name, send in (("email", lambda: send_email(subject, body)),
                       ("slack", lambda: send_slack(f"*{subject}*\n{body}"))):
        try:
            send()
            delivered.append(name)
        except Exception as exc:                       # noqa: BLE001 - report, never crash
            failed.append(f"{name}: {type(exc).__name__}")

    if delivered:
        record(delivered)
        print("delivered on " + ", ".join(delivered))
        return 0
    print("no channel delivered: " + "; ".join(failed), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
