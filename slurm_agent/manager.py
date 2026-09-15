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
# # Talking to the manager
#
# Everything past setup is a conversation with the manager, not a sequence of commands. So
# there is one way to reach it from a script or a notebook — `poe ask` — and it is the same
# manager you would get by opening Claude Code in this repo: same `CLAUDE.md`, same
# `.claude/skills/slurm-orchestration/SKILL.md`, same `poe` surface.
#
# That sameness is the whole design, and two things protect it:
#
# * **The session runs in the repo root**, so `CLAUDE.md` and the project skills are
#   discovered the way they are in an interactive session. Never `--bare`: it skips
#   `CLAUDE.md` discovery, restricts auth to `ANTHROPIC_API_KEY`, and skips hooks.
# * **The prompt is prefixed**, because auto-discovery makes a skill *available* and a
#   preamble makes it *used*. A manager that has not read its own skill will happily invent
#   a worse procedure, and you will not be able to tell from the reply.
#
# The reply comes back as text on stdout, so a caller can read it or parse ids out of it:
#
# ```bash
# IDS=$(poe ask "Open two Notion tasks: … and … . Reply with the two ids, one per line.")
# poe ask "Run $IDS on Tillicum. Keep me posted on progress and spend."
# ```

# %%
import json

import structlog
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict

from slurm_agent.config import ManagerConfig
from slurm_agent.remote import Runner, quote

log = structlog.get_logger(__name__)


class ManagerError(RuntimeError):
    """The manager session did not come back with a usable answer."""


class Reply(BaseModel):
    """What one manager session returned."""

    model_config = ConfigDict(extra="forbid")

    text: str
    cost_usd: float
    session_id: str | None = None


# %%
# Availability is not use. A skill is offered to a session; a preamble is what makes it the
# first thing read, and the difference shows up as a manager improvising a procedure that
# already exists and is better.
PREAMBLE = """You are the manager for this repository, running on the researcher's laptop.

Before doing anything else, read `.claude/skills/slurm-orchestration/SKILL.md` — it is the
procedure for everything to do with Tillicum, tasks and remote agents, and it is not
optional. `CLAUDE.md` in this directory is the design it follows, and `poe --help` is the
full inventory of what you can drive. The Notion Tasks database is named in
`config/tasks.yaml`.

Then do what is asked below. If it involves Tillicum or Notion, do it yourself through
those tools rather than telling the human which commands to run.

---

"""


def manager_argv(cfg: ManagerConfig, prompt: str, *, resume: str | None = None) -> list[str]:
    """The exact `claude` command line. Pure — this is the audit of what it may do."""
    argv = ["claude", "-p", PREAMBLE + prompt, "--output-format", "json"]
    if resume:
        argv += ["--resume", resume]
    argv += [
        # Nobody is watching this session, so no prompt may block it.
        "--permission-mode", "dontAsk",
        "--max-budget-usd", str(cfg.max_budget_usd),
    ]
    if cfg.allowed_tools:
        argv += ["--allowed-tools", *cfg.allowed_tools]
    if cfg.mcp:
        # Both together, always: --mcp-config without --strict would leave the laptop's
        # ambient servers reachable, which the declared config is meant to bound.
        argv += ["--mcp-config", str(cfg.mcp_config), "--strict-mcp-config"]
    if cfg.model:
        argv += ["--model", cfg.model]
    return argv


# %%
if test():
    cfg = ManagerConfig()
    argv = manager_argv(cfg, "Open two tasks.")

    assert argv[0] == "claude" and argv[1] == "-p"
    # The preamble rides in front of every prompt, so the skill is read, not merely offered.
    assert argv[2].startswith("You are the manager")
    assert "slurm-orchestration/SKILL.md" in argv[2]
    assert argv[2].endswith("Open two tasks.")
    # Never --bare: it skips CLAUDE.md discovery, which is where the design lives.
    assert "--bare" not in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    display(argv[3:])


# %%
if test():
    resumed = manager_argv(ManagerConfig(), "and now?", resume="4f2c")
    assert resumed[resumed.index("--resume") + 1] == "4f2c"
    # Resuming still carries the preamble; a continued session that has drifted off the
    # skill is exactly the case where restating it is worth the tokens.
    assert resumed[2].startswith("You are the manager")


# %%
def ask(cfg: ManagerConfig, prompt: str, run: Runner, *, resume: str | None = None) -> Reply:
    """Run one headless manager session and return what it said.

    `run` is a laptop `Runner` — the same seam the cluster side uses, so this needs no
    mocking library and the caller can see exactly what was executed.
    """
    argv = manager_argv(cfg, prompt, resume=resume)
    raw = run(" ".join(quote(a) for a in argv))
    try:
        result = json.loads(raw[raw.index("{"):])
    except (ValueError, json.JSONDecodeError) as exc:
        raise ManagerError(
            f"could not read the session's output: {raw.strip()[:300]!r}") from exc
    if result.get("is_error"):
        raise ManagerError(f"the manager session failed: {str(result.get('result'))[:300]}")

    reply = Reply(text=str(result.get("result", "")).strip(),
                  cost_usd=float(result.get("total_cost_usd") or 0),
                  session_id=result.get("session_id"))
    log.info("manager.replied", cost_usd=reply.cost_usd, session=reply.session_id)
    return reply


# %%
if test():
    from tests.conftest import FakeRunner

    ok = FakeRunner({"claude": json.dumps(
        {"result": "TASK-118\nTASK-119", "total_cost_usd": 0.21,
         "session_id": "abc123", "is_error": False})})
    reply = ask(ManagerConfig(), "Open two tasks.", ok)
    assert reply.text == "TASK-118\nTASK-119"
    assert reply.cost_usd == 0.21 and reply.session_id == "abc123"
    display(reply.model_dump())


# %%
if test():
    # A failed session must not read as an answer — its `result` is an error message, and
    # returning it would put "budget exceeded" where a task id was expected.
    refusals = []
    for bad, expect in ((json.dumps({"result": "budget exceeded", "is_error": True}),
                         "the manager session failed"),
                        ("command not found: claude", "could not read")):
        try:
            ask(ManagerConfig(), "go", FakeRunner({"claude": bad}))
            raise AssertionError(f"{bad[:30]!r} should have raised")
        except ManagerError as error:
            assert expect in str(error)
            refusals.append(str(error)[:90])
    display(refusals)
