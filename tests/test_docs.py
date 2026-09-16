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


def test_the_quick_start_shows_commands_rather_than_running_them_for_you():
    """Setup is two commands; everything after is a `claude` you could have typed.

    A python helper wrapping the commands hides the one thing worth seeing, and it makes
    the notebook prove something you cannot repeat in a terminal. So every runnable cell is
    a bang magic, and there is no helper to run them through.
    """
    src = (ROOT / "docs" / "quickstart.py").read_text()
    assert "def sh(" not in src and "subprocess" not in src, "no wrapper around the commands"
    assert "poe ask" not in src and "slurm-agent ask" not in src

    # Setup is init, then hc until green. Nothing else is run for you.
    assert "# !uv run poe init" in src
    assert "# !uv run poe hc --full --send" in src
    for command in ("poe job-up", "poe agent-run", "poe agent-batch", "poe agent-watch",
                    "poe job-down", "poe flush", "poe status", "poe agent-logs"):
        assert f"# !uv run {command}" not in src, f"the quick start runs {command} itself"

    # And the interface it teaches is a task id, not a pile of arguments.
    assert "Pick up TASK-118 on Tillicum" in src
    assert "A task id is enough" in src


def _quickstart_sessions() -> list[str]:
    """The prompt of every `claude -p` cell in the quick start."""
    import re
    import shlex

    src = (ROOT / "docs" / "quickstart.py").read_text()
    prompts = []
    for line in src.splitlines():
        if not line.startswith("# !claude "):
            continue
        words = shlex.split(line[len("# !"):])
        assert words[:2] == ["claude", "-p"], words[:2]
        assert len(words) == 3, f"the prompt split into pieces: {words}"
        prompts.append(words[2])
    return prompts


def test_the_quick_starts_claude_commands_parse_as_shell_commands():
    """The commands are the deliverable now, so they must be right, not just look right.

    An apostrophe in a prompt — "what it's doing" — ends the single-quoted string and the
    rest becomes shell syntax. That fails in a way that reads like Claude misbehaving
    rather than like a quoting bug.
    """
    sessions = _quickstart_sessions()
    assert len(sessions) == 3, f"expected three sessions, found {len(sessions)}"
    # Open the tasks, run them, ask how they are going.
    assert "Reply with the two task ids" in sessions[0]
    assert "on Tillicum" in sessions[1]
    assert all("TASK-118" in s for s in sessions[1:]), "the ids must carry between sessions"


def test_the_manager_skill_grounds_work_in_notion():
    """A run with no task id is a run nobody can find afterwards.

    The chain has four links and the skill has to state all of them, because each one is
    invisible from the next: the manager opens or reads the task, the id reaches the
    launch, the agent opens that row, and the agent writes back to it.
    """
    skill = (ROOT / ".claude" / "skills" / "slurm-orchestration" / "SKILL.md").read_text()
    assert "config/tasks.yaml" in skill, "it must say where the Tasks database is named"
    assert "open the rows yourself" in " ".join(skill.split()), \
        "the human may describe work with no row yet; the manager files it"
    assert 'poe agent-run "$TASK_A"' in skill, "the id must be shown reaching the launch"
    assert "Never launch without a task id" in skill
    assert "{{ task }}" in skill, "it must say which brief variable the id becomes"
    assert "the ids, one per line, no sentence around them" in skill


def test_the_agent_brief_reads_and_updates_its_task():
    """The other end of the chain. The id is useless if the agent ignores it."""
    brief = " ".join((ROOT / "prompts" / "agent_launch.md.jinja").read_text().split())
    assert "Open it first" in brief and "Notion MCP" in brief
    assert "Update {{ task }} in Notion" in brief
    # And it must refuse rather than guess, because working under the wrong row files real
    # effort against someone else's record.
    assert "cannot find {{ task }} in Notion" in brief


def test_running_claude_from_the_repo_root_is_enough_to_be_the_manager():
    """No wrapper configures the session. The repo does, where Claude Code looks.

    `claude` from this root has to come up as the manager on its own, because that is what
    a human types and what every doc now shows. Three files make it so, and each is silent
    when missing: `.mcp.json` (Notion reachable), `.claude/settings.json` (enabled without
    a prompt, and `poe` pre-approved), and `CLAUDE.md` (what it is for).
    """
    import json

    mcp = json.loads((ROOT / ".mcp.json").read_text())
    assert "notion" in mcp["mcpServers"], "the manager could not open a task"

    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    assert settings.get("enableAllProjectMcpServers") is True, \
        "the servers would need approving by hand on every fresh clone"
    allow = settings["permissions"]["allow"]
    assert any("poe" in rule for rule in allow), "it drives this repo through poe"

    claude_md = (ROOT / "CLAUDE.md").read_text()
    assert "you are the manager" in claude_md.lower()
    assert "slurm-orchestration/SKILL.md" in claude_md


def test_the_repo_config_holds_no_secrets_and_protects_the_one_file_that_does():
    """Both JSONs are committed. `.envrc` is the only local file with real values in it."""
    import json

    for name in (".mcp.json", ".claude/settings.json"):
        text = (ROOT / name).read_text()
        for marker in ("ghp_", "sk-ant", "xoxb-", "Bearer ", "PASSWORD", "TOKEN="):
            assert marker not in text, f"{name} looks like it carries a credential"

    deny = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    assert any(".envrc" in rule for rule in deny), \
        "the manager has no reason to read secrets; poe puts them in the environment for it"


def test_the_manager_skill_knows_the_agent_is_somewhere_else():
    """The manager is on a laptop; the agent is on a compute node. It must say so.

    An agent that thinks it is where the manager is opens files that are not there, runs
    control-plane commands, and pushes from a machine nobody checked. The shipped briefs
    state it — a brief the manager WRITES has to as well, and only the skill can tell it.
    """
    skill = (ROOT / ".claude" / "skills" / "slurm-orchestration" / "SKILL.md").read_text()
    assert "Tillicum compute node" in skill
    assert "it must do the same" in skill, "a new brief has to carry the same context"
    assert "SLURM_AGENT_RUN_DIR" in skill and "needs_human" in skill


def test_the_manager_skill_expects_a_task_id_and_nothing_else():
    """"Pick up TASK-118 on Tillicum" is the whole interface, so it must be enough."""
    skill = (ROOT / ".claude" / "skills" / "slurm-orchestration" / "SKILL.md").read_text()
    assert "A task id is the whole brief you get" in skill
    assert "do not ask the human to repeat what Notion already says" in skill
