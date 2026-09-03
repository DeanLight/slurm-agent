"""The status shim, exercised the way the cluster runs it: as a subprocess.

It lives in `slurm_agent/assets/` and is never imported here — it runs under whatever
python the experiment repo has, so it must work with nothing but the standard library.
"""

import json
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
    path.write_text(json.dumps({"cells": cells, "nbformat": 4}))
    return path


def test_agent_update_records_the_semantic_fields(tmp_path):
    assert run_shim(tmp_path, "running", "--round", "2/3").returncode == 0
    block = json.loads((tmp_path / "status.json").read_text())
    assert block["state"] == "running"
    assert block["round"] == "2/3"
    assert block["updated"].endswith("Z")


def test_tick_refreshes_liveness_without_erasing_what_the_agent_said(tmp_path):
    """The hook knows the world, the agent knows the meaning; neither overwrites the other."""
    run_shim(tmp_path, "needs_env", "--round", "2/3", "--waiting-on", "HF_TOKEN")
    nb = notebook_with(4, tmp_path / "run.ipynb")
    assert run_shim(tmp_path, "tick", "--notebook", str(nb)).returncode == 0

    block = json.loads((tmp_path / "status.json").read_text())
    assert block["cells_done"] == 4          # observed, not reported
    assert block["round"] == "2/3"           # the agent's half survives
    assert block["waiting_on"] == ["HF_TOKEN"]
    assert block["state"] == "needs_env"


def test_tick_on_a_fresh_run_creates_a_valid_block(tmp_path):
    nb = notebook_with(1, tmp_path / "run.ipynb")
    assert run_shim(tmp_path, "tick", "--notebook", str(nb)).returncode == 0
    block = json.loads((tmp_path / "status.json").read_text())
    assert block["state"] == "running" and block["cells_done"] == 1


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
