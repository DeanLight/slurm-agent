"""Every committed config must load into the model that reads it.

This is the check that was missing when `TaskConfig` lost three fields and
`config/tasks.yaml` kept them: `extra="forbid"` turned a stale config into a traceback at
`poe hc`, on a machine that was correctly set up, for a file nobody had touched. The models
are exercised all over the suite — with values constructed in the test — so nothing noticed
that the YAML on disk no longer matched.

A config is only as good as the moment it is read, and that moment is on someone's laptop.
"""

from pathlib import Path

import pytest

from slurm_agent.config import (
    AgentConfig,
    ClusterConfig,
    ManagerConfig,
    MonitorConfig,
    SupervisionConfig,
    load,
)
from slurm_agent.tasks import TaskConfig

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = {
    "cluster.yaml": ClusterConfig,
    "manager.yaml": ManagerConfig,
    "monitor.yaml": MonitorConfig,
    "supervision.yaml": SupervisionConfig,
    "tasks.yaml": TaskConfig,
}


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_each_committed_config_loads(name):
    """The file on disk, through the real loader, into the real model."""
    load(ROOT / "config" / name, CONFIGS[name])


def test_every_committed_config_is_covered_here():
    """A new config/*.yaml must be added above, or it ships unvalidated.

    The failure mode is not a broken test — it is a file nobody loads until a user does.
    """
    shipped = {p.name for p in (ROOT / "config").glob("*.yaml")}
    assert shipped == set(CONFIGS), f"unchecked: {sorted(shipped - set(CONFIGS))}"


@pytest.mark.parametrize("path", sorted((ROOT / "agents").glob("*.yaml")))
def test_each_committed_agent_loads(path):
    load(path, AgentConfig)
