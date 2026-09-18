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
from tests.conftest import FakeRunner

ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = (ROOT / "docs" / "quickstart.py").read_text()

# Every row name the quick start cites. Adding a row is free; renaming one must either
# update the doc or fail here.
CITED = ["reachable", "my keys", "agent credential", "allocation probe",
         "ssh config", ".envrc", "run root", "clone", "worktree", "tmux",
         "claude auth", "mcp notion", "mcp github"]


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
        full=True, envrc=ROOT / "templates" / "envrc.example",
        env={"SLURM_AGENT_TEST_KEY": "s"}, ssh_dir=ROOT / "ssh_config_templates",
        local=runner,
        tasks=__import__("slurm_agent.tasks", fromlist=["TaskConfig"]).TaskConfig(
            data_source="collection://abc"))


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
    assert "# !uv run poe hc --full" in src
    # And the work is one bash block you paste, not a cell you edit first.
    assert src.count('# %% language="bash"') == 1


def test_the_quick_start_hands_over_to_an_interactive_manager():
    """`poe manage` is taught without `-p`, and says what to ask it.

    `-p` prints and exits, so a quick start built out of `-p` calls teaches a thing that is
    not the interface: no conversation to come back to, and — since Remote Control is
    interactive-only — nothing on your phone. It earns its place once, to capture task ids
    into a shell variable, and the block that uses it ends by printing the plain
    `poe manage` command to paste next.
    """
    src = (ROOT / "docs" / "quickstart.py").read_text()
    body = "\n".join(_quickstart_bash())
    assert body.count("poe manage -p") == 1, "`-p` is the exception, not the lesson"
    assert "poe manage \"Pick up these tasks on Tillicum" in body, (
        "the block must print the interactive command, with the real ids already in it")

    # And the notebook says what to type at that prompt, rather than leaving you there.
    assert "ask it things" in src
    assert "How are my runs going, and what have they cost so far?" in src
    for command in ("poe job-up", "poe agent-run", "poe agent-batch", "poe agent-watch",
                    "poe job-down", "poe flush", "poe status", "poe agent-logs"):
        assert f"# !uv run {command}" not in src, f"the quick start runs {command} itself"

    # And the interface it teaches is a task id, not a pile of arguments.
    assert "Pick up TASK-118 on Tillicum" in src
    assert "A task id is enough" in src


def _quickstart_bash() -> list[str]:
    """The body of every `%%bash` cell, as a terminal would receive it.

    jupytext stores a cell magic as `# %% language="bash"` with the body commented out, so
    the .py stays valid Python. What a user pastes is the uncommented body — this
    reconstructs exactly that.
    """
    blocks: list[str] = []
    body: list[str] | None = None
    for line in (ROOT / "docs" / "quickstart.py").read_text().splitlines():
        if line.startswith('# %% language="bash"'):
            body = []
            continue
        if body is None:
            continue
        if line.startswith("# %%"):
            blocks.append("\n".join(body))
            body = None
            continue
        body.append(line[2:] if line.startswith("# ") else line.lstrip("#"))
    if body is not None:
        blocks.append("\n".join(body))
    return blocks


def test_the_quick_starts_bash_runs_as_pasted():
    """It has to work unchanged in a terminal: no placeholder to edit, no syntax error.

    A block containing `TASK-118` made the reader stop and substitute — exactly the friction
    the manager exists to remove. And an apostrophe in a prompt would end the single-quoted
    string and turn the rest into shell syntax, which reads like Claude misbehaving rather
    than like a quoting bug.
    """
    import subprocess

    blocks = _quickstart_bash()
    assert len(blocks) == 1, f"expected one pasteable block, found {len(blocks)}"

    (opener,) = blocks
    done = subprocess.run(["bash", "-n", "-c", opener], capture_output=True, text=True)
    assert done.returncode == 0, f"not valid bash:\n{opener}\n{done.stderr}"

    # The ids go into a variable and straight into the command printed for you — nothing to
    # fill in, and no second window where a shell variable would not exist.
    assert "IDS=$(poe manage -p" in opener
    assert 'echo "opened: $IDS"' in opener
    assert "$IDS" in opener
    assert "TASK-1" not in opener, "a placeholder id means the reader has to edit it"
    # And what it prints is the INTERACTIVE form. A quick start whose last word is `-p`
    # leaves you with no conversation to come back to and nothing on your phone.
    printed = opener.split("cat <<", 1)[1]
    assert "poe manage \"Pick up these tasks on Tillicum" in printed
    assert "-p" not in printed.split("\n")[1]


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
