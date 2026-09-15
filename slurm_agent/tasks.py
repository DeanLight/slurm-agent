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
# is *about* a task, and the task is what carries the record afterwards. The manager opens
# the rows itself, through its own Notion MCP — this module is only the two things around
# that which must not be done by feel.
#
# **Which database.** `config/tasks.yaml` names it, so the manager reads one file rather
# than being told the id in a prompt each time.
#
# **Reading ids back out of prose.** You ask the manager for two tasks and it replies with
# a sentence containing two ids, or one, or none. `extract_ids` takes exactly what you
# expected and refuses otherwise. That refusal is the point: launching an agent against the
# wrong task is worse than not launching, because it does real work and files it under
# someone else's row.

# %%
import re

import structlog
from IPython.display import display
from juplit import test
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger(__name__)


class TaskError(RuntimeError):
    """The reply did not contain the task ids it was supposed to."""


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
    # A one-line reminder of what a row here is for, shown to the manager so it fills in
    # the same fields every time rather than inventing a schema per run.
    notes: str = ""


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
def extract_ids(text: str, pattern: str, count: int) -> list[str]:
    """The `count` task ids in `text`, in the order they appear. Anything else is an error.

    The manager is asked for N ids and usually gives exactly N. When it does not — it made
    one row, or three, or described what it would do without doing it — that is a real
    disagreement about what happened, and guessing which ids it meant would turn a visible
    problem into runs filed under the wrong rows.
    """
    found = list(dict.fromkeys(m.group(1) for m in re.finditer(pattern, text)))
    if len(found) == count:
        return found
    raise TaskError(
        f"expected {count} task ids matching {pattern!r}, found {len(found)}: "
        f"{found or 'none'} — the manager said: {text.strip()[:300]!r}"
    )


# %%
if test():
    pat = TaskConfig(data_source="x").id_pattern

    assert extract_ids("TASK-118\nTASK-119", pat, 2) == ["TASK-118", "TASK-119"]
    # Prose around them is fine, and order is preserved — the caller pairs them with the
    # tasks it asked for, in the order it asked.
    assert extract_ids("Opened TASK-118 (retry) and TASK-119 (docs).", pat, 2) == \
        ["TASK-118", "TASK-119"]

    refusals = []
    for bad, want in (("TASK-118", 2), ("TASK-1 TASK-2 TASK-3", 2), ("done!", 2)):
        try:
            extract_ids(bad, pat, want)
            raise AssertionError(f"{bad!r} should not have resolved")
        except TaskError as error:
            refusals.append(str(error)[:100])
    display(refusals)
