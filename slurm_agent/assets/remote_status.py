#!/usr/bin/env python3
"""The status block writer, copied into each run root at launch.

Runs under whatever python the experiment repo has, so it takes no dependencies and does
no imports beyond the standard library.

Three verbs:

    tick    --log-dir DIR    hook: refresh `updated`, find the notebook being worked in,
                             recount its cells. Needs nothing from the agent.
    finish  --log-dir DIR    hook: write the terminal state.
    <state> [--round R] [--waiting-on K ...]   the agent's own semantic update.

An analysis grows several notebooks over time, so nothing here is told which one is
current: `current_notebook` picks the most recently modified `.ipynb` under the log dir
and records it, which is how the supervisor knows what to watch without asking the agent.

Every write is atomic (temp file plus os.replace), so a kill mid-write leaves the previous
block readable rather than half a file.
"""

import json
import os
import sys
import tempfile
import time

TERMINAL = {"finished", "failed"}


def status_path() -> str:
    return os.path.join(os.environ.get("SLURM_AGENT_RUN_DIR", "."), "status.json")


def read() -> dict:
    try:
        with open(status_path()) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write(block: dict) -> None:
    block["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    target = status_path()
    directory = os.path.dirname(target) or "."
    handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(handle, "w") as out:
        json.dump(block, out, indent=1, sort_keys=True)
    os.replace(tmp, target)


def current_notebook(log_dir: str) -> str:
    """The notebook the agent is working in: the most recently modified one in the log dir.

    Observed, not declared. An agent that starts a second notebook is followed
    automatically, and one that never creates any yields "" rather than a wrong answer.
    """
    best, best_mtime = "", -1.0
    for root, _dirs, names in os.walk(log_dir):
        for name in names:
            if not name.endswith(".ipynb") or ".ipynb_checkpoints" in root:
                continue
            path = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
    return best


def count_cells(notebook: str) -> int:
    """How many cells carry outputs — an observed fact, not something the agent reports."""
    try:
        with open(notebook) as handle:
            cells = json.load(handle).get("cells", [])
    except (OSError, ValueError):
        return 0
    return sum(1 for c in cells if c.get("outputs"))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    flags = {}
    key = None
    for item in rest:
        if item.startswith("--"):
            key = item[2:].replace("-", "_")
            flags.setdefault(key, [])
        elif key:
            flags[key].append(item)

    block = read()
    log_dir = (flags.get("log_dir") or [block.get("log_dir", "")])[0]
    if log_dir:
        block["log_dir"] = log_dir
        notebook = current_notebook(log_dir)
        if notebook:
            block["notebook"] = notebook
            block["cells_done"] = count_cells(notebook)

    if verb == "remind":
        # The reminder is a HOOK, not a line in the system prompt, so it arrives in the
        # agent's context at the moment it matters rather than once at launch. It only
        # speaks when the agent has actually fallen behind: cells have been executed since
        # the last semantic update, or no round was ever recorded.
        write(block)
        stale = block.get("cells_done", 0) > block.get("cells_at_last_report", 0)
        if not stale and block.get("round"):
            return 0
        nb = os.path.basename(block.get("notebook", "")) or "your notebook"
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"slurm-agent: you are {block.get('cells_done', 0)} cells into {nb} and your "
                f"last recorded round is {block.get('round') or 'unset'}. Record it now: "
                "python3 $SLURM_AGENT_RUN_DIR/remote_status.py running --round N/TOTAL "
                "(or needs_env / needs_human --waiting-on ... if you are blocked). "
                "Liveness is tracked for you; only you know the round."
            ),
        }}))
        return 0

    if verb == "tick":
        # Liveness only. Never clears `round` or `waiting_on`: the hook knows the world,
        # the agent knows the meaning, and neither should overwrite the other's half.
        block.setdefault("state", "running")
    elif verb == "finish":
        state = (flags.get("state") or ["finished"])[0]
        block["state"] = state if state in TERMINAL else "failed"
        block["waiting_on"] = []
    else:
        block["state"] = verb
        if "round" in flags:
            block["round"] = flags["round"][0]
        if "waiting_on" in flags:
            block["waiting_on"] = flags["waiting_on"]
        # Remember where the agent was when it last reported, so the reminder hook can
        # tell "has not spoken since real work happened" from "spoke a moment ago".
        block["cells_at_last_report"] = block.get("cells_done", 0)
    write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
