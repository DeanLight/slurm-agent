# slurm-agent

## If you are reading this, you are the manager

You were started in this repo, which means someone wants Tillicum work done. **Read
`.claude/skills/slurm-orchestration/SKILL.md` before doing anything else** — it is the
procedure for tasks, allocations, launches, supervision and teardown, and it is not
optional. `poe --help` is the full inventory of what you can drive.

The human names the work. You do the rest: open the Notion task, size the compute, write
the agent config, launch, supervise, report progress and spend, tear down. If you find
yourself telling them which `poe` command to run, you have handed back the job.

When you are asked for ids and nothing else, give exactly that — one per line, no sentence
around them. The caller is a shell capturing your reply into a variable, and prose becomes
part of the id.

---

A local Claude Code session brings up Tillicum allocations, stages repos, and launches and
supervises Claude agents on the compute node. Tillicum sits behind UW 2FA on a network only
the researcher's laptop is on, so **this repo only works from that laptop** — no sandbox,
cloud session or CI runner can reach the cluster.

Full project context lives in Notion; read the **Dev Workspace** page through the Notion MCP
before starting, as every other repo here does.

## The one rule that shapes everything

**The cluster is the only source of truth. The laptop holds nothing it cannot rebuild.**

Every fact this repo reports — what is allocated, how far a run got, what it cost, what
failed — is derived from the SLURM queue plus files on the cluster's shared filesystem.

Do not add a local run registry, a state database, or a "dismissed" list. If you find
yourself wanting one, the answer is on the cluster: `squeue` for live jobs, `sacct` for
finished ones, and the run root (`~/.slurm-agent/runs/<session_id>/`) for everything about
an agent. That rule is what makes a closed laptop lossless and two sessions agree.

## Two rules that follow from it

- **Nothing this repo writes ever lands inside a staged repo.** `stage()` refuses to launch
  onto a dirty tree, and that refusal is what stops an experiment silently measuring
  unreviewed code — so our own files must never be what dirties it. Launch records, status
  blocks, hook settings and logs all live in the run root, addressed absolutely via
  `SLURM_AGENT_RUN_DIR`. The notebook is the one deliberate exception: it is the deliverable,
  and the agent commits it.
- **Committed YAML names environment keys, never values.** Secrets live in the gitignored
  `.envrc` on each machine; `requires_env` is how a config says what it needs. `poe` loads
  `.envrc` for every task via `[tool.poe] envfile`, so there is no credentials reader here
  and no `direnv` dependency.
- **Every message names one of three places.** *This laptop* (this checkout, `~/.ssh/config`,
  the keys for reaching you), *the login node* (run root, tmux, the Claude credential), and
  *each staged repo on the cluster* (the keys that agent declares). They do not share files
  and they do not share keys: `config/manager.yaml` is the laptop's, `agents/<kind>.yaml` is
  that repo's. A `Check` carries `where`, `render` groups by it, and a row that cannot say
  which machine it means is a bug — asking the laptop for an agent's `HF_TOKEN` failed a
  correctly-configured machine and sent people to fill in a file nothing reads.
- **Git auth is checked on both machines, and staging is not setup.** The laptop and the
  login node hold different GitHub credentials, and the login node's is the one that
  decides whether an agent can push the notebook a GPU-hour produced — so `hc` asks both,
  and `--full` adds a `--dry-run` push, because `ls-remote` succeeds on a public repo with
  no credential at all. A workdir, by contrast, is created by the first *launch*: "not
  cloned yet" is the normal state of a fresh clone and must never render as a fault.
- **A key is required because something reads it, never because a list says so.** Which
  notification keys the laptop needs is derived from `config/notify.yaml`'s `channels` via
  `notify.CHANNEL_KEYS`, which the senders themselves read through. `manager.requires_env`
  is empty on purpose. A hand-kept list is wrong in both directions, and the quiet
  direction is the dangerous one: turn Slack on without updating it and `hc` passes while
  the escalation never arrives. A key with a default (`SMTP_PORT`) is reported, never
  failed. Optional means OFF: `config/notify.yaml` ships `channels: [email]`, because a
  channel that is on but cannot send is worse than one that is off — you find out when
  nothing arrives.
- **The report is the only output.** No inventory beside it, and no separate account of
  what was created: the group headings already name every place and every managed repo, and
  `init` returns notes — keyed by (place, row) — that fold into the row they are about.
  `init` never reports a failure of its own, because the check seconds later says the same
  thing better; saying it twice is how a missing login node arrived as four lines of ssh
  askpass noise.
- **`agents/*.yaml` IS the list of managed repos.** One file per agent, naming repo, ref and
  workdir. That is the whole registry, and it follows from the one rule: nothing is
  remembered between runs, so delete the file and the repo is no longer managed.
- **Every shipped agent points at this repo and declares no keys.** They are examples, and
  a fork must not inherit a config naming a repo it cannot clone or a token it has no use
  for — either turns a correctly-set-up laptop red with no fix available to its owner. The
  one repo a fork can always clone and push to is itself. Two tests pin this; declare keys
  and real repos on an agent you actually run, not on the examples.

## Working here

- `poe --help` is the inventory. Every capability is a `poe` task wrapping one
  `slurm-agent` command, so an agent driving this repo uses the same surface a human does.
- `poe init` **creates** the local footprint; `poe healthcheck` (alias `poe hc`) **verifies**
  it and creates nothing. `hc` is fast on purpose — run it after moving network or
  re-authing to Tillicum, where a dropped `ControlMaster` is the usual culprit.
- `docs/quickstart.py` is **setup, and it stops there**: `init`, fill `.envrc`, `hc --full
  --send`, then the `claude` commands to type. Every runnable cell is a bang magic, so the
  notebook shows the real command rather than running it through a helper — a wrapper would
  hide the only thing worth seeing and prove something you cannot repeat in a terminal. It
  is the one notebook this repo commits with outputs, because those outputs are the proof a
  clone works.
- **The repo configures the manager, not a wrapper.** `claude` started in this root comes
  up as the manager because of four files Claude Code reads by itself: this one, the skill,
  `.mcp.json` (Notion and GitHub) and `.claude/settings.json` (those servers pre-enabled,
  `poe` pre-approved, `.envrc` denied). Anything a manager session needs goes in those, so
  that what a human types and what a script runs are the same command. A wrapper that
  configured the session would work while a bare `claude` quietly did not.
- **A reply is stdout and nothing else.** `IDS=$(claude -p "…")` is the contract that lets
  one session feed the next — `--output-format` defaults to `text` under `-p` — so logs go
  to stderr repo-wide. A log line captured into that variable does not fail; it asks the
  next session to run a task named after a timestamp.
- **Work is grounded in Notion.** The manager opens the rows itself through its own MCP, in
  the database `config/tasks.yaml` names; the id becomes `{{ task }}` in the agent's brief,
  the agent opens that row before starting and writes its findings back to it.
  `extract_ids` takes exactly the number asked for and refuses otherwise — a run filed under
  the wrong row is worse than no run.
- **The human names the work; the manager does the rest.** Every `poe` command exists so
  that an agent can drive this repo through the same surface a human would — not so a human
  has to. A skill or doc that tells the human to run `poe job-up` has handed back the job it
  was given.
- **How much compute a task needs is the manager's call, and `gpus:` is how an agent states
  its half of it.** `gpus: 0` means "claims no device", which is what lets several small
  tasks share one allocation as steps — Tillicum permits one interactive allocation, so a
  second job is not a tidier answer, it is an unavailable one. Batch is for a task that
  needs a node for hours or runs unattended. `.claude/skills/slurm-orchestration/SKILL.md` is that
  decision written down, and it is what an agent asked to "run this on Tillicum" follows.
- Which brief an agent gets is its own declared property (`prompt:` in `agents/*.yaml`,
  a template in `prompts/`). Every brief takes the same variables, which is what lets one
  launcher carry a twelve-hour experiment agent and a two-minute smoke agent.
- Source lives in `slurm_agent/` as jupytext `py:percent` paired notebooks with `if test():`
  blocks beside each function. Read the juplit skill (`poe skill`) before editing one.
- Everything that touches the cluster takes a `Runner` (see `slurm_agent/remote.py`). That
  single seam is why the suite needs no mocking library — tests pass a dict-backed fake,
  and a test asserts nothing outside `remote.py` (and `monitor.py`, for the local crontab)
  imports `subprocess`. A path around the seam is a path the smoke suite cannot fake.
- **`tests/test_smoke.py` runs every command against a fake cluster**, through the real CLI
  and the real committed configs. `tests/fake_cluster.py` syntax-checks every command with
  `bash -n` before answering, because "valid shell" is exactly what an ssh'd command has to
  be — an unquoted `|` in a `--Format` string reached a user twice, and the fake found the
  second one. Both runner factories are replaced with something that FAILS, so a smoke run
  cannot quietly shell out: the first version of that fixture patched `local_runner` in the
  module that defines it rather than the one that imports it, and spent real tokens on
  `claude -p` every run. Add a command to `COMMANDS` when you add one — a test fails if you
  do not.

## Remote paths use `$HOME`, never `~`

Commands are shell-quoted before they cross the ssh boundary, and `shlex.quote` quotes `~`
— so a tilde reaches the remote shell literally and never expands. Config files may be
written with `~` for readability, but anything interpolated into a remote command must be
resolved to `$HOME` first.
