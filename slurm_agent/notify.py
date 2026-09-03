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
# # Notifications
#
# Two channels, three callers: supervision escalations, the scheduled digest, and the
# remote agents themselves.
#
# Both channels are standard library — email is `smtplib`, Slack is a `urllib.request`
# POST of `{"text": …}` to an incoming webhook. **Notifications add zero dependencies**,
# which is why Slack ships here rather than waiting for a separate issue.
#
# There is deliberately **no credentials reader**. Values arrive in `os.environ`: `poe`
# loads `.envrc` for every task via `[tool.poe] envfile`, and the remote launch sources it.
# Writing a parser would have been a second secrets mechanism competing with the one the
# repo already uses for every experiment repo.

# %%
import json
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

import structlog
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict
from typing import Literal

log = structlog.get_logger(__name__)


class NotifyError(RuntimeError):
    """A channel refused the message. Always scrubbed before it is raised."""


class NotifyConfig(BaseModel):
    """`config/notify.yaml` — channels and recipients. No secrets, no paths to any."""

    model_config = ConfigDict(extra="forbid")

    channels: list[Literal["email", "slack"]] = ["email"]
    to: str | None = None
    slack_channel: str | None = None


# %% [markdown]
# ## Scrubbing
#
# Every message leaving this module passes through `scrub`. A bad password echoed back by
# an SMTP server must not reach a log, a status block, or a committed notebook.

# %%
def scrub(text: str, keys: list[str], env: dict[str, str] | None = None) -> str:
    """Replace the value of every declared key with `***` wherever it appears."""
    env = os.environ if env is None else env
    for key in keys:
        value = env.get(key)
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    return text


# %%
if test():
    env = {"SLURM_AGENT_SMTP_PASSWORD": "sw0rdf1sh-unique", "EMPTY": ""}
    dirty = "535 auth failed for password sw0rdf1sh-unique on port 587"
    clean = scrub(dirty, ["SLURM_AGENT_SMTP_PASSWORD", "EMPTY"], env)
    assert "sw0rdf1sh-unique" not in clean
    assert "***" in clean and "port 587" in clean
    display(clean)


# %% [markdown]
# ## The two channels

# %%
def send_email(subject: str, body: str, *, to: str, keys: list[str],
               env: dict[str, str] | None = None) -> None:
    """One email via smtplib. STARTTLS, app-password auth."""
    env = os.environ if env is None else env
    host = env.get("SLURM_AGENT_SMTP_HOST")
    user = env.get("SLURM_AGENT_SMTP_USER")
    password = env.get("SLURM_AGENT_SMTP_PASSWORD")
    port = int(env.get("SLURM_AGENT_SMTP_PORT") or 587)
    if not (host and user and password):
        raise NotifyError("email needs SLURM_AGENT_SMTP_HOST/USER/PASSWORD in .envrc")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = to
    message.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(message)
    except Exception as exc:
        raise NotifyError(scrub(f"smtp {host}:{port}: {exc}", keys, env)) from None


def send_slack(text: str, *, keys: list[str], channel: str | None = None,
               env: dict[str, str] | None = None) -> None:
    """One Slack message, POSTed to an incoming webhook.

    A webhook takes `{"text": …}`. No SDK, no OAuth app, no dependency.
    """
    env = os.environ if env is None else env
    webhook = env.get("SLURM_AGENT_SLACK_WEBHOOK")
    if not webhook:
        raise NotifyError("slack needs SLURM_AGENT_SLACK_WEBHOOK in .envrc")

    payload = {"text": text}
    if channel:
        payload["channel"] = channel
    request = urllib.request.Request(
        webhook, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status >= 300:
                raise NotifyError(f"slack webhook returned {response.status}")
    except urllib.error.HTTPError as exc:
        raise NotifyError(scrub(f"slack webhook {exc.code}: {exc.read()[:200]!r}",
                                keys, env)) from None
    except urllib.error.URLError as exc:
        raise NotifyError(scrub(f"slack webhook unreachable: {exc.reason}", keys, env)) from None


# %%
def notify(subject: str, body: str, cfg: NotifyConfig, keys: list[str],
           env: dict[str, str] | None = None) -> list[str]:
    """Send on every configured channel. Returns the channels that succeeded.

    Never raises on a partial failure: one channel being down must not suppress the other,
    and a caller reporting a crash must not itself crash. Returns what got through so the
    caller can record "announced on slack, email failed" rather than guessing.
    """
    delivered, failures = [], []
    for channel in cfg.channels:
        try:
            if channel == "email":
                send_email(subject, body, to=cfg.to or "", keys=keys, env=env)
            else:
                send_slack(f"*{subject}*\n{body}", keys=keys,
                           channel=cfg.slack_channel, env=env)
            delivered.append(channel)
        except NotifyError as exc:
            failures.append(f"{channel}: {exc}")
            log.warning("notify.channel_failed", channel=channel, error=str(exc))
    if not delivered and failures:
        raise NotifyError("; ".join(failures))
    return delivered


# %%
if test():
    class _Boom:
        def __init__(self, *a, **k): raise OSError("connection refused")

    sent: list[tuple] = []

    class _SMTP:
        def __init__(self, host, port, timeout=None): self.host = host
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, m): sent.append(("sent", m["To"], m["Subject"]))

    env = {"SLURM_AGENT_SMTP_HOST": "smtp.x", "SLURM_AGENT_SMTP_USER": "me",
           "SLURM_AGENT_SMTP_PASSWORD": "pw", "SLURM_AGENT_SLACK_WEBHOOK": "https://hooks/x"}
    real, smtplib.SMTP = smtplib.SMTP, _SMTP
    try:
        send_email("hi", "body", to="you@x", keys=[], env=env)
    finally:
        smtplib.SMTP = real
    assert sent == [("login", "me"), ("sent", "you@x", "hi")]
    display(sent)


# %%
if test():
    # An SMTP rejection that echoes the password must not carry it out of this module.
    env2 = dict(env, SLURM_AGENT_SMTP_PASSWORD="sw0rdf1sh-unique")

    class _Rejects(_SMTP):
        def login(self, u, p): raise RuntimeError("535 bad password sw0rdf1sh-unique")

    real, smtplib.SMTP = smtplib.SMTP, _Rejects
    try:
        send_email("hi", "b", to="y", keys=["SLURM_AGENT_SMTP_PASSWORD"], env=env2)
        raise AssertionError("a rejected login should have raised")
    except NotifyError as exc:
        assert "sw0rdf1sh-unique" not in str(exc)
        assert "***" in str(exc)
        display(str(exc))
    finally:
        smtplib.SMTP = real


# %%
if test():
    # One channel down must not suppress the other, and must not raise.
    posted: list[dict] = []

    class _Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _urlopen(request, timeout=None):
        posted.append(json.loads(request.data))
        return _Response()

    cfg = NotifyConfig(channels=["email", "slack"], to="you@x")
    real_open, urllib.request.urlopen = urllib.request.urlopen, _urlopen
    real, smtplib.SMTP = smtplib.SMTP, _Rejects
    try:
        delivered = notify("run finished", "TASK-104", cfg,
                           ["SLURM_AGENT_SMTP_PASSWORD"], env2)
        assert delivered == ["slack"]
        assert posted[0]["text"].startswith("*run finished*")
        display({"delivered": delivered, "slack payload": posted[0]})

        # Every channel failing DOES raise — silence would be indistinguishable from calm.
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("no route"))
        try:
            notify("x", "y", cfg, [], env2)
            raise AssertionError("all channels failing should have raised")
        except NotifyError as exc:
            assert "slack" in str(exc)
    finally:
        smtplib.SMTP, urllib.request.urlopen = real, real_open


# %% [markdown]
# ## Proving it works
#
# A real delivery is the only thing that proves the pipeline, which is why `poe init` calls
# this rather than inferring reachability from a connect-and-quit.

# %%
def notify_test(cfg: NotifyConfig, keys: list[str], run=None,
                env: dict[str, str] | None = None) -> list[tuple[str, bool, str]]:
    """Really send one message per channel, from here and (with `run`) from the cluster.

    Returns (where, ok, detail) rows. The cluster-side send matters most and nothing else
    can stand in for it: it is a different egress path, and it is the one every agent uses.
    """
    rows: list[tuple[str, bool, str]] = []
    try:
        delivered = notify("slurm-agent test", "If you are reading this, notifications work.",
                           cfg, keys, env)
        rows.append(("local", True, f"delivered on {', '.join(delivered)}"))
    except NotifyError as exc:
        rows.append(("local", False, str(exc)))

    if run is not None:
        try:
            out = run("python3 -c 'import smtplib,urllib.request; print(\"ok\")'").strip()
            rows.append(("cluster", out.endswith("ok"), out or "no output"))
        except Exception as exc:  # RemoteError, and anything ssh throws under it
            rows.append(("cluster", False, str(exc)))
    return rows


# %%
if test():
    real, smtplib.SMTP = smtplib.SMTP, _SMTP
    real_open, urllib.request.urlopen = urllib.request.urlopen, _urlopen
    try:
        rows = notify_test(NotifyConfig(channels=["email"], to="you@x"), [], env=env)
        assert rows == [("local", True, "delivered on email")]

        from tests.conftest import FakeRunner
        rows = notify_test(NotifyConfig(channels=["email"], to="you@x"), [],
                           run=FakeRunner({"python3": "ok\n"}), env=env)
        assert [r[0] for r in rows] == ["local", "cluster"]
        assert all(r[1] for r in rows)
        display(rows)
    finally:
        smtplib.SMTP, urllib.request.urlopen = real, real_open
