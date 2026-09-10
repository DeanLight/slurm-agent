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


def _agents() -> list[AgentConfig]:
    return [load(p, AgentConfig) for p in sorted((ROOT / "agents").glob("*.yaml"))]


def test_every_agent_brief_exists_and_renders():
    """A missing or under-parameterised brief is a launch-time failure on a paid GPU.

    Every brief takes the same variables, which is what lets one launcher put a twelve-hour
    experiment agent and a two-minute smoke agent on the same allocation. `render` uses
    StrictUndefined, so a brief that reaches for a variable the launcher does not pass
    fails here rather than after the allocation is up.
    """
    from slurm_agent.launch import launch_prompt

    for agent in _agents():
        assert (ROOT / "prompts" / agent.prompt).exists(), f"{agent.prompt} is missing"
        text = launch_prompt(agent, task="SMOKE-1", log_dir="smoke/smoke-1",
                             run_dir="/home/d/.slurm-agent/runs/4f2c", sha="a1b2c3d4e5")
        assert "SMOKE-1" in text
        assert "remote_status.py" in text
        # No brief may tell an agent to cancel an allocation it shares with others.
        assert "Do not cancel the allocation" in text


def test_the_smoke_brief_stays_small():
    """The smoke brief's whole value is being cheap and deterministic.

    Two things would quietly destroy that: sending it to Notion to be routed like a real
    experiment agent, and telling it to execute a notebook, which makes a sanity check
    depend on a kernel that may not be installed. Both have been written into it before.
    """
    # Normalised, because these promises are prose and prose gets re-wrapped.
    brief = " ".join((ROOT / "prompts" / "smoke.md.jinja").read_text().split())
    assert "Do not read Notion" in brief
    assert "Do not try to execute a notebook" in brief
    smoke = load(ROOT / "agents" / "smoke.yaml", AgentConfig)
    assert smoke.requires_env == [], "a smoke test that needs a credential has two ways to fail"
    assert smoke.max_budget_usd <= 1


def test_the_two_smoke_halves_share_a_branch_but_not_a_tree():
    """One PR, two staged checkouts.

    Sharing the branch is the point — it is what puts both halves in one pull request.
    Sharing a *workdir* would break them: `stage()` refuses to launch onto a dirty tree, so
    the second half would be refused while the first still had an uncommitted notebook, and
    a `git checkout -B` from either would yank the branch from under the other.
    """
    a = load(ROOT / "agents" / "smoke.yaml", AgentConfig)
    b = load(ROOT / "agents" / "smoke-batch.yaml", AgentConfig)
    assert (a.repo, a.ref) == (b.repo, b.ref)
    assert a.workdir != b.workdir
    assert (a.mode, b.mode) == ("interactive", "batch")
