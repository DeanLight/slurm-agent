"""The status shim, exercised the way the cluster runs it: as a subprocess.

It lives in `slurm_agent/assets/` and is never imported here — it runs under whatever
python the experiment repo has, so it must work with nothing but the standard library.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SHIM = Path(__file__).resolve().parent.parent / "slurm_agent" / "assets" / "remote_status.py"


def run_shim(run_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SHIM), *args], capture_output=True, text=True,
        env={"SLURM_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"},
    )


def notebook_with(cells_with_outputs: int, path: Path) -> Path:
    cells = [{"cell_type": "code", "source": "x", "outputs": [{"output_type": "stream"}]}
             for _ in range(cells_with_outputs)]
    cells.append({"cell_type": "code", "source": "y", "outputs": []})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cells": cells, "nbformat": 4}))
    return path


def log_dir_with(tmp_path: Path, **notebooks: int) -> Path:
    """An experiment log dir holding several notebooks, written in the order given."""
    logs = tmp_path / "experiments" / "exp14"
    for index, (name, cells) in enumerate(notebooks.items()):
        nb = notebook_with(cells, logs / f"{name}.ipynb")
        os.utime(nb, (1_000_000 + index * 60, 1_000_000 + index * 60))
    return logs


def test_agent_update_records_the_semantic_fields(tmp_path):
    assert run_shim(tmp_path, "running", "--round", "2/3").returncode == 0
    block = json.loads((tmp_path / "status.json").read_text())
    assert block["state"] == "running"
    assert block["round"] == "2/3"
    assert block["updated"].endswith("Z")


def test_tick_refreshes_liveness_without_erasing_what_the_agent_said(tmp_path):
    """The hook knows the world, the agent knows the meaning; neither overwrites the other."""
    run_shim(tmp_path, "needs_env", "--round", "2/3", "--waiting-on", "HF_TOKEN")
    logs = log_dir_with(tmp_path, run=4)
    assert run_shim(tmp_path, "tick", "--log-dir", str(logs)).returncode == 0

    block = json.loads((tmp_path / "status.json").read_text())
    assert block["cells_done"] == 4          # observed, not reported
    assert block["round"] == "2/3"           # the agent's half survives
    assert block["waiting_on"] == ["HF_TOKEN"]
    assert block["state"] == "needs_env"


def test_tick_on_a_fresh_run_creates_a_valid_block(tmp_path):
    logs = log_dir_with(tmp_path, run=1)
    assert run_shim(tmp_path, "tick", "--log-dir", str(logs)).returncode == 0
    block = json.loads((tmp_path / "status.json").read_text())
    assert block["state"] == "running" and block["cells_done"] == 1


def test_the_current_notebook_is_discovered_not_declared(tmp_path):
    """An analysis grows several notebooks; the newest one is the one being worked in."""
    logs = log_dir_with(tmp_path, first_pass=9, follow_up=2)
    run_shim(tmp_path, "tick", "--log-dir", str(logs))

    block = json.loads((tmp_path / "status.json").read_text())
    assert block["notebook"].endswith("follow_up.ipynb")   # newest, not largest
    assert block["cells_done"] == 2
    assert block["log_dir"] == str(logs)


def test_a_log_dir_with_no_notebook_yet_reports_no_notebook(tmp_path):
    """Better an empty answer than a wrong one: an absent notebook is not zero progress."""
    logs = tmp_path / "experiments" / "exp14"
    logs.mkdir(parents=True)
    assert run_shim(tmp_path, "tick", "--log-dir", str(logs)).returncode == 0
    block = json.loads((tmp_path / "status.json").read_text())
    assert "notebook" not in block and "cells_done" not in block


def test_remind_speaks_only_when_the_agent_has_fallen_behind(tmp_path):
    """The reminder is a hook, so it must be quiet unless there is something to say."""
    logs = log_dir_with(tmp_path, run=5)
    run_shim(tmp_path, "tick", "--log-dir", str(logs))

    # Nothing reported yet, and five cells have run: it should prompt.
    out = run_shim(tmp_path, "remind", "--log-dir", str(logs)).stdout
    payload = json.loads(out)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PostToolUse"
    assert "remote_status.py running --round" in payload["additionalContext"]
    assert "run.ipynb" in payload["additionalContext"]

    # After the agent reports, the same state is no longer behind — stay silent.
    run_shim(tmp_path, "running", "--round", "1/3")
    assert run_shim(tmp_path, "remind", "--log-dir", str(logs)).stdout.strip() == ""

    # More cells run: behind again, so speak again.
    notebook_with(8, logs / "run.ipynb")
    run_shim(tmp_path, "tick", "--log-dir", str(logs))
    assert "additionalContext" in run_shim(tmp_path, "remind", "--log-dir", str(logs)).stdout


def test_finish_writes_a_terminal_state_and_clears_the_block(tmp_path):
    run_shim(tmp_path, "needs_human", "--waiting-on", "a decision")
    run_shim(tmp_path, "finish", "--state", "finished")
    block = json.loads((tmp_path / "status.json").read_text())
    assert block["state"] == "finished"
    assert block["waiting_on"] == []


def test_finish_refuses_a_non_terminal_state(tmp_path):
    """A `finish` that is not finished is a failure — never silently `running`."""
    run_shim(tmp_path, "finish", "--state", "running")
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "failed"


def test_a_write_leaves_no_temp_file_behind(tmp_path):
    """The write is atomic: temp plus rename, so a kill mid-write leaves the old block."""
    run_shim(tmp_path, "running", "--round", "1/3")
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_an_unreadable_block_is_replaced_rather_than_crashing(tmp_path):
    (tmp_path / "status.json").write_text("{ half written")
    assert run_shim(tmp_path, "running").returncode == 0
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "running"
