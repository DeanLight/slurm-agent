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


def _every_pair() -> dict[str, str]:
    """Assignments in the template, commented ones included.

    A commented example is still a line someone will uncomment and a line a leak can hide
    in, so it is held to the same rule as a live one.
    """
    pairs = {}
    for line in TEMPLATE.read_text().splitlines():
        line = line.strip().lstrip("#").strip()
        if not line or "=" not in line or " " in line.partition("=")[0]:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def test_template_holds_no_real_values():
    """Every committed value is the placeholder, never a credential.

    The shipped template declares no LIVE key at all — the manager needs none and no
    shipped agent declares one — so the guard is over the commented examples too.
    Otherwise this test would pass by having nothing to check, which is how a leak gets in
    later.
    """
    pairs = _every_pair()
    assert pairs, "template shows no keys at all, not even an example"
    offenders = {k: v for k, v in pairs.items() if v != SECRET_PLACEHOLDER}
    assert not offenders, f"non-placeholder values in the committed template: {sorted(offenders)}"


def test_template_covers_exactly_the_declared_keys():
    """The example matches every key this repo can ask for, from wherever it is declared.

    Two sources, and the template is the one place both are visible: the manager's own
    `requires_env` and each agent's. The shipped repo declares neither, so the expected
    set is empty and the template is all comments — which is the point. A fork that adds
    a key to a config and not to the template fails here.
    """
    manager = load(ROOT / "config" / "manager.yaml", ManagerConfig)
    expected = sorted(declared_env_keys(manager, _agents()))
    assert sorted(_template_pairs()) == expected


def test_envrc_is_gitignored():
    """The real file must never be committable — the template is the committed half."""
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert any(re.fullmatch(r"\.envrc/?", line.strip()) for line in ignored)


def _agents_by_kind() -> dict[str, AgentConfig]:
    return {p.stem: load(p, AgentConfig) for p in sorted((ROOT / "agents").glob("*.yaml"))}


def _agents() -> list[AgentConfig]:
    return list(_agents_by_kind().values())


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


def test_the_two_trial_tasks_differ_only_in_their_workdir():
    """Same repo, same ref, same brief, same budget — two tasks, not two modes.

    How each one runs is the manager's call at launch, so neither may pre-decide it. The
    separate workdirs are not tidiness: `stage()` runs `git fetch` and `checkout --detach`
    there, and two launches racing in one checkout is the kind of failure that looks like a
    cluster problem for an hour.
    """
    a = load(ROOT / "agents" / "smoke.yaml", AgentConfig)
    b = load(ROOT / "agents" / "smoke-2.yaml", AgentConfig)
    assert (a.repo, a.ref, a.prompt, a.max_budget_usd) == (b.repo, b.ref, b.prompt,
                                                           b.max_budget_usd)
    assert a.workdir != b.workdir


def test_no_shipped_agent_needs_a_branch_to_be_created_first():
    """Setup steps with no payoff are how a "two-minute" trial becomes an afternoon.

    Nothing in this repo creates a branch any more — the trial tasks only read what they
    stage — so a ref naming one would fail at `git clone --branch` on a fresh machine, for
    a branch whose only purpose was to have been created.
    """
    for kind, agent in _agents_by_kind().items():
        assert agent.ref == "main", f"agents/{kind}.yaml stages {agent.ref!r}, which nothing creates"


def test_the_trial_tasks_claim_no_gpu_and_cannot_touch_a_repo():
    """Two small tasks must fit on ONE allocation, and must need no branch, PR or push.

    `gpus: 0` is what makes them steps on a shared allocation rather than two jobs on a
    cluster that permits one interactive allocation. Their output goes under the run root
    and they hold no git tools, so there is nothing to review on GitHub to see this work —
    `poe hc` already proves push on both machines with a dry run.
    """
    for kind in ("smoke", "smoke-2"):
        agent = load(ROOT / "agents" / f"{kind}.yaml", AgentConfig)
        assert agent.gpus == 0, f"{kind} would force an allocation of its own"
        assert agent.log_dir.startswith("{RUN_DIR}"), f"{kind} writes into the staged repo"
        assert not any("git" in tool for tool in agent.allowed_tools), kind


def test_every_shipped_agent_points_at_this_repo():
    """A fork must not inherit a config naming a repo it cannot clone.

    Every `agents/*.yaml` here is an example. If one pointed at a private repo of mine,
    a fresh fork's `poe hc` would fail on a staged repo it has no access to and no way to
    fix — and the fix would be to know what my repo was. The one repo a fork is always
    allowed to clone and push to is itself, so that is where the examples point.
    """
    for kind, agent in _agents_by_kind().items():
        assert agent.repo == "DeanLight/slurm-agent", (
            f"agents/{kind}.yaml points at {agent.repo} — a fork cannot use that")


def test_shipped_agents_need_no_credentials():
    """A fresh fork is green once its OWN keys are filled, with nothing staged yet.

    An example agent that declares `HF_TOKEN` turns a correctly-set-up laptop red on a key
    it has no use for, in a cluster-side file it has no reason to have created yet. Declare
    keys on an agent you actually run, not on the examples.
    """
    for kind, agent in _agents_by_kind().items():
        assert agent.requires_env == [], f"agents/{kind}.yaml declares keys a fork lacks"
