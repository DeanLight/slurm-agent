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
# # Tasks
#
# Work here is grounded in the Notion Tasks database: a run is worth something because it
# is *about* a task, and a task is what carries the record afterwards. So the manager opens
# the task before it launches anything, and hands the agent the task's id.
#
# The id has to cross a shell boundary. The manager writes
#
# ```bash
# TASK_A=$(poe task-new "Add a retry to the loader")
# poe agent-run "$TASK_A" --job dev --agent task-a
# ```
#
# and `{{ task }}` reaches the agent's brief, which is how a remote agent knows which task
# it is doing and which row to update when it is done. That shapes everything below:
#
# * **stdout is the id and nothing else.** Not a sentence containing the id, not JSON to be
#   parsed by the caller — one token, so `$( )` is enough and no one has to write a parser
#   in a prompt. Everything a human wants to read goes to stderr.
# * **The model is not trusted to format.** It is asked for the id, and `extract_id` pulls
#   exactly one match out of whatever it said. Zero matches or several is an error, never a
#   guess — launching an agent against the wrong task is worse than not launching.
# * **The argv is the audit surface**, as it is for a remote agent: `task_argv` is pure, and
#   reading it tells you the session may reach Notion and nothing else.

# %%
import json
import re
from pathlib import Path

import structlog
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict

from slurm_agent.remote import Runner, quote

log = structlog.get_logger(__name__)


class TaskError(RuntimeError):
    """The headless session did not come back with exactly one task id."""


# %%
class TaskConfig(BaseModel):
    """`config/tasks.yaml` — which Notion database work is grounded in."""

    model_config = ConfigDict(extra="forbid")

    # The Notion data source the Tasks database lives in, as a `collection://…` url. Get it
    # by fetching the database through the Notion MCP once; it does not change.
    data_source: str
    # What a task id looks like, as a regex with exactly one group. `extract_id` requires
    # exactly one distinct match, so this must be specific enough not to match prose.
    id_pattern: str = r"\b((?:TASK-|T\d-)\d+)\b"
    # Set on every task this repo opens, so a row is always attributable to a project.
    defaults: dict[str, str] = {}
    # The laptop-side MCP config. Only servers named here are reachable, because
    # `--strict-mcp-config` goes with it.
    mcp_config: Path = Path("config/mcp.json")
    mcp_servers: list[str] = ["notion"]
    max_budget_usd: float = 1.0


# %%
def task_prompt(cfg: TaskConfig, title: str, body: str | None = None) -> str:
    """What the headless session is asked to do. One job, one line back."""
    fields = "\n".join(f"  - {k}: {v}" for k, v in cfg.defaults.items())
    return (
        f"Create ONE new task in the Notion database at {cfg.data_source}.\n\n"
        f"Title: {title}\n"
        + (f"\nDescription for the page body:\n{body}\n" if body else "")
        + (f"\nSet these properties:\n{fields}\n" if fields else "")
        + "\nFetch the data source first to get its exact property names, then create the "
        "row. Do not create more than one. Do not modify any existing row.\n\n"
        "Then reply with the new task's id and NOTHING else — no sentence around it, no "
        "markdown, no explanation. Just the id."
    )


def task_argv(cfg: TaskConfig, prompt: str) -> list[str]:
    """The exact `claude` command line. Pure — this is the audit of what it may do."""
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        # Nobody is watching this session, so no prompt may block it.
        "--permission-mode", "dontAsk",
        "--max-budget-usd", str(cfg.max_budget_usd),
        # Both together, always: --mcp-config without --strict would leave the laptop's
        # ambient MCP servers reachable, which the declared config is meant to bound.
        "--mcp-config", str(cfg.mcp_config), "--strict-mcp-config",
    ]
    if cfg.mcp_servers:
        argv += ["--allowed-tools", *[f"mcp__{name}" for name in cfg.mcp_servers]]
    return argv


# %%
if test():
    cfg = TaskConfig(data_source="collection://abc", defaults={"Project": "slurm-agent"})
    argv = task_argv(cfg, "go")

    assert argv[:3] == ["claude", "-p", "go"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    # Strict, so the session reaches Notion and nothing else — not the laptop's other
    # servers, and not the filesystem.
    assert "--strict-mcp-config" in argv
    assert "mcp__notion" in argv
    assert "Read" not in argv and "Bash" not in " ".join(argv)
    display(argv)


# %%
if test():
    text = task_prompt(cfg, "Add a retry to the loader", body="It flakes on 429s.")
    assert "collection://abc" in text
    assert "Project: slurm-agent" in text
    assert "It flakes on 429s." in text
    # The two instructions that make the output usable from `$( )`.
    assert "NOTHING else" in text
    assert "Do not create more than one" in text
    display(text)


# %% [markdown]
# ## Reading the id back
#
# A model asked for one token sometimes sends a sentence anyway. That is fine — what is not
# fine is guessing which number in the sentence was the id.

# %%
def extract_id(text: str, pattern: str) -> str:
    """The one task id in `text`. Zero or several is an error, never a guess."""
    found = list(dict.fromkeys(m.group(1) for m in re.finditer(pattern, text)))
    if len(found) == 1:
        return found[0]
    raise TaskError(
        f"expected exactly one task id matching {pattern!r}, found {len(found)}: "
        f"{found or 'none'} — the session said: {text.strip()[:300]!r}"
    )


# %%
if test():
    pat = TaskConfig(data_source="x").id_pattern

    assert extract_id("TASK-118", pat) == "TASK-118"
    # A sentence around it is fine…
    assert extract_id("Created TASK-118 in the Tasks database.", pat) == "TASK-118"
    # …and so is the same id said twice.
    assert extract_id("TASK-118 — see TASK-118", pat) == "TASK-118"
    assert extract_id("T2-104", pat) == "T2-104"

    # Two DIFFERENT ids must not resolve. Launching an agent against the wrong task is
    # worse than not launching: it does real work and files it under someone else's row.
    refusals = []
    for bad in ("TASK-118 and TASK-119", "no id here", "created 3 rows"):
        try:
            extract_id(bad, pat)
            raise AssertionError(f"{bad!r} should not have resolved")
        except TaskError as error:
            assert "exactly one" in str(error)
            refusals.append(str(error)[:110])
    display(refusals)


# %%
def create_task(cfg: TaskConfig, title: str, run: Runner, *,
                body: str | None = None) -> tuple[str, float]:
    """Open one Notion task headlessly. Returns (task_id, cost_usd).

    `run` is a laptop `Runner` — the same seam the cluster side uses, which is why this
    needs no mocking library to test and why the caller can see exactly what was executed.
    """
    argv = task_argv(cfg, task_prompt(cfg, title, body))
    raw = run(" ".join(quote(a) for a in argv))
    try:
        result = json.loads(raw[raw.index("{"):])
    except (ValueError, json.JSONDecodeError) as exc:
        raise TaskError(f"could not read the session's output: {raw.strip()[:300]!r}") from exc

    if result.get("is_error"):
        raise TaskError(f"the session failed: {str(result.get('result'))[:300]}")
    task_id = extract_id(str(result.get("result", "")), cfg.id_pattern)
    cost = float(result.get("total_cost_usd") or 0)
    log.info("task.created", task=task_id, cost_usd=cost)
    return task_id, cost


# %%
if test():
    from tests.conftest import FakeRunner

    ok = FakeRunner({"claude": json.dumps(
        {"result": "TASK-118", "total_cost_usd": 0.042, "is_error": False})})
    task_id, cost = create_task(cfg, "Add a retry to the loader", ok)
    assert (task_id, cost) == ("TASK-118", 0.042)
    # The prompt really carried the title, and the session was bounded.
    assert "Add a retry to the loader" in ok.commands[0]
    assert "--strict-mcp-config" in ok.commands[0]
    display(task_id)


# %%
if test():
    # A failed session must not look like a created task.
    broke = FakeRunner({"claude": json.dumps(
        {"result": "budget exceeded", "is_error": True, "total_cost_usd": 1.0})})
    seen = []
    try:
        create_task(cfg, "t", broke)
        raise AssertionError("an errored session must raise")
    except TaskError as error:
        assert "the session failed" in str(error)
        seen.append(str(error)[:90])

    # And neither must output that is not JSON at all.
    try:
        create_task(cfg, "t", FakeRunner({"claude": "command not found: claude"}))
        raise AssertionError("unreadable output must raise")
    except TaskError as error:
        assert "could not read" in str(error)
        seen.append(str(error)[:90])
    display(seen)
