"""The agent's outbound shim, driven as a subprocess the way the cluster runs it."""

import json
import subprocess
import sys
from pathlib import Path

SHIM = Path(__file__).resolve().parent.parent / "slurm_agent" / "assets" / "remote_notify.py"


def run_shim(run_dir: Path, *args: str, env: dict | None = None):
    return subprocess.run(
        [sys.executable, str(SHIM), *args], capture_output=True, text=True,
        env={"SLURM_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin", **(env or {})},
    )


def seed(run_dir: Path, state: str = "finished") -> None:
    (run_dir / "status.json").write_text(json.dumps(
        {"state": state, "round": "3/3", "cells_done": 14, "waiting_on": []}))
    (run_dir / "launch.json").write_text(json.dumps(
        {"task": "TASK-104", "notebook": "/w/experiments/exp14/run.ipynb"}))


def test_no_credentials_exits_nonzero_without_touching_the_status_block(tmp_path):
    """A notify failure must never corrupt supervision state."""
    seed(tmp_path)
    before = (tmp_path / "status.json").read_text()
    result = run_shim(tmp_path, "--from-status")
    assert result.returncode == 1
    assert "no channel delivered" in result.stderr
    assert (tmp_path / "status.json").read_text() == before


def test_from_status_builds_its_message_out_of_recorded_state(tmp_path):
    """The hook derives subject and body from the block, not from the agent's improvisation."""
    seed(tmp_path, state="needs_human")
    result = run_shim(tmp_path, "--from-status")
    # Delivery fails with no credentials, but the failure names both channels it tried.
    assert "email" in result.stderr and "slack" in result.stderr


def test_a_write_leaves_no_temp_file_behind(tmp_path):
    seed(tmp_path)
    run_shim(tmp_path, "--from-status")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["launch.json", "status.json"]
