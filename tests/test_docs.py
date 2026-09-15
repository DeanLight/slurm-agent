"""The quick start names rows of the setup report. Those names must be real.

This is the drift that keeps happening, and it is invisible: prose citing a row that was
renamed or merged away still reads perfectly, and only fails the person following it at
the moment they are already stuck. So the doc's row names are checked against the ones
`healthcheck` can actually produce.
"""

import re
from pathlib import Path

from slurm_agent import preflight
from slurm_agent.config import AgentConfig, ClusterConfig, ManagerConfig
from slurm_agent.notify import NotifyConfig
from tests.conftest import FakeRunner

ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = (ROOT / "docs" / "quickstart.py").read_text()

# Every row name the quick start cites. Adding a row is free; renaming one must either
# update the doc or fail here.
CITED = ["reachable", "my keys", "notify send", "agent credential", "allocation probe",
         "ssh config", ".envrc", "run root", "clone", "worktree", "tmux", "task database"]


def _every_row() -> list[preflight.Check]:
    """One healthcheck exercising every branch, so every producible row appears."""
    agent = AgentConfig(repo="DeanLight/slurm-agent", ref="main", workdir="~/work/x",
                        log_dir="e", max_budget_usd=1, requires_env=["HF_TOKEN"])
    runner = FakeRunner({
        "id -un": "d\n", "test -d": "repo\n", "tmux -V": "tmux 3.3a\n",
        "remote get-url": "https://github.com/DeanLight/slurm-agent\n",
        "status --porcelain": "", "stat -c": "600\n", "test -f": "yes",
        "ls-remote": "a\trefs/heads/main\n", "push --dry-run": "up to date\n",
        "tmux new-session": "held\n", "claude": '{"total_cost_usd": 0.01}',
        "cat": "export HF_TOKEN=real\n",
    })
    return preflight.healthcheck(
        ClusterConfig(login_host="h"), ManagerConfig(), {"k": agent}, runner,
        full=True, send=True, envrc=ROOT / "templates" / "envrc.example",
        env={"SLURM_AGENT_SMTP_HOST": "s"}, ssh_dir=ROOT / "ssh_config_templates",
        notify=NotifyConfig(channels=["email"]), local=runner,
        tasks=__import__("slurm_agent.tasks", fromlist=["TaskConfig"]).TaskConfig(
            data_source="collection://abc"),
        notify_test=lambda: [("local", True, "sent"), ("cluster", True, "sent")])


def test_the_quick_start_only_names_rows_that_exist():
    produced = {c.name for c in _every_row()}
    missing = [name for name in CITED if not any(p == name or p.startswith(f"{name} ")
                                                 for p in produced)]
    assert not missing, f"docs/quickstart.py names rows healthcheck no longer produces: {missing}"


def test_the_quick_start_cites_every_row_it_should():
    """The other direction, loosely: a row nobody documents is a row nobody will read."""
    undocumented = [name for name in CITED if f"`{name}`" not in QUICKSTART]
    assert not undocumented, f"CITED lists rows the quick start does not mention: {undocumented}"


def test_the_quick_start_names_no_stale_paths():
    """Workdirs and repos move. Prose that hard-codes one goes stale silently."""
    from slurm_agent.config import load_agents

    real = {a.workdir for a in load_agents(ROOT / "agents").values()}
    for path in re.findall(r"~/work/[\w.-]+", QUICKSTART):
        assert path in real, f"docs/quickstart.py names {path}, which no agent stages into"


def test_every_project_skill_is_where_claude_code_looks_for_it():
    """Claude Code discovers project skills as `.claude/skills/<name>/SKILL.md`.

    A flat `.claude/skills/<name>.md` is not an error and not a warning — it is silently
    never loaded, which looks exactly like a skill that exists and is being ignored. The
    manager skill is the one that has to auto-load for "run this on Tillicum" to work at
    all, so its layout is worth a test rather than a memory.
    """
    skills = ROOT / ".claude" / "skills"
    flat = [p.name for p in skills.glob("*.md")]
    assert not flat, f"these would never load; each needs its own directory + SKILL.md: {flat}"
    assert (skills / "slurm-orchestration" / "SKILL.md").exists()


def test_the_manager_skill_says_how_to_size_compute():
    """The decision the human delegated is the one the skill must actually make.

    "Run these two tasks on Tillicum" is answerable only if the skill states the rule: one
    interactive allocation exists, `gpus: 0` tasks are steps on it, and batch is for work
    that needs a node to itself.
    """
    skill = (ROOT / ".claude" / "skills" / "slurm-orchestration" / "SKILL.md").read_text()
    assert "poe hc --full" in skill, "the manager must check both machines before spending"
    assert "permits one interactive allocation" in skill
    assert "`gpus:`" in skill and "agent-batch" in skill
    assert "pull request" in skill, "it must say not to send the human to a PR"


def test_docs_name_the_skill_by_its_real_path():
    """A doc pointing at the old flat path teaches the layout that does not work."""
    for doc in ("README.md", "CLAUDE.md", "docs/quickstart.py"):
        text = (ROOT / doc).read_text()
        assert "skills/slurm-orchestration.md" not in text, doc


def test_the_manager_skill_takes_the_whole_job():
    """The human names the work. Everything else is the manager's.

    The failure this guards against is subtle and was real: a skill that lists `poe`
    commands reads like a manual, and an agent following it hands the commands back to the
    human instead of running them. It has to say, in words, that doing so is the job
    returned.
    """
    skill = (ROOT / ".claude" / "skills" / "slurm-orchestration" / "SKILL.md").read_text()
    assert "you do all of it" in skill
    assert "Report in your own words, unprompted" in skill
    assert "Never hand the mechanics back" in skill
    assert "Never leave an allocation up" in skill


def test_the_quick_start_only_talks_to_claude_after_setup():
    """Past `hc`, every cell asks the manager. None of them does the work itself.

    Driving allocations and launches from a notebook is a slower, more brittle copy of what
    the manager does, and it made setup look as though it required a trial run to succeed.
    """
    src = (ROOT / "docs" / "quickstart.py").read_text()
    for command in ("poe job-up", "poe agent-run", "poe agent-batch", "poe agent-watch",
                    "poe job-down", "poe flush", "poe status", "poe agent-logs"):
        assert f"sh('uv run {command}" not in src and f'sh("uv run {command}' not in src, \
            f"the quick start still runs {command} itself"
    assert "poe hc --full --send" in src, "it must still prove the machines"
    assert "you only talk to Claude" in src
    # The two asks: open the tasks, then run them. The second must carry the ids from the
    # first, which is the whole shape the human asked for.
    assert "uv run poe ask" in src
    assert "extract_ids(" in src and "{TASK_A} and {TASK_B}" in src


def test_the_manager_skill_grounds_work_in_notion():
    """A run with no task id is a run nobody can find afterwards.

    The chain has four links and the skill has to state all of them, because each one is
    invisible from the next: the manager opens the task, the id crosses a shell boundary in
    a variable, the launch puts it in the agent's brief, and the agent writes back to that
    same row.
    """
    skill = (ROOT / ".claude" / "skills" / "slurm-orchestration" / "SKILL.md").read_text()
    assert "config/tasks.yaml" in skill, "it must say where the Tasks database is named"
    assert "open the rows yourself" in skill, "the human describes work; the manager files it"
    assert 'poe agent-run "$TASK_A"' in skill, "the id must be shown reaching the launch"
    assert "Never launch without a task id" in skill
    assert "{{ task }}" in skill, "it must say which brief variable the id becomes"
    # Asked for ids and nothing else, the caller is a script.
    assert "the ids, one per line, no sentence around them" in skill


def test_the_agent_brief_reads_and_updates_its_task():
    """The other end of the chain. The id is useless if the agent ignores it."""
    brief = " ".join((ROOT / "prompts" / "agent_launch.md.jinja").read_text().split())
    assert "Open it first" in brief and "Notion MCP" in brief
    assert "Update {{ task }} in Notion" in brief
    # And it must refuse rather than guess, because working under the wrong row files real
    # effort against someone else's record.
    assert "cannot find {{ task }} in Notion" in brief
