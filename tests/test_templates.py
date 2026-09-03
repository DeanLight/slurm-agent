"""Guards on the committed env template.

Two failure modes are worth a test each, because both are silent:

* a real credential reaching the committed example — the one way a copyable template
  becomes a leak;
* the example drifting out of step with the keys the configs actually declare, which turns
  setup into a scavenger hunt through source.
"""

import re
from pathlib import Path

from slurm_agent.config import (
    SECRET_PLACEHOLDER,
    AgentConfig,
    ManagerConfig,
    declared_env_keys,
    load,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "envrc.example"


def _template_pairs() -> dict[str, str]:
    pairs = {}
    for line in TEMPLATE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def test_template_holds_no_real_values():
    """Every committed value is the placeholder, never a credential."""
    pairs = _template_pairs()
    assert pairs, "template defines no keys"
    offenders = {k: v for k, v in pairs.items() if v != SECRET_PLACEHOLDER}
    assert not offenders, f"non-placeholder values in the committed template: {sorted(offenders)}"


def test_template_covers_exactly_the_declared_keys():
    """The example matches the union of every requires_env in the repo."""
    manager = load(ROOT / "config" / "manager.yaml", ManagerConfig)
    agents = [load(p, AgentConfig) for p in sorted((ROOT / "agents").glob("*.yaml"))]
    assert sorted(_template_pairs()) == declared_env_keys(manager, agents)


def test_envrc_is_gitignored():
    """The real file must never be committable — the template is the committed half."""
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert any(re.fullmatch(r"\.envrc/?", line.strip()) for line in ignored)
