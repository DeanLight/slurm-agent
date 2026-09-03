#!/usr/bin/env python3
"""The status block writer, copied into each run root at launch.

Runs under whatever python the experiment repo has, so it takes no dependencies and does
no imports beyond the standard library.

Three verbs:

    tick    --notebook NB     hook: refresh `updated`, recount cells. No agent needed.
    finish  --notebook NB     hook: write the terminal state.
    <state> [--round R] [--waiting-on K ...]   the agent's own semantic update.

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
    notebook = (flags.get("notebook") or [block.get("notebook", "")])[0]
    if notebook:
        block["notebook"] = notebook
        block["cells_done"] = count_cells(notebook)

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
    write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
