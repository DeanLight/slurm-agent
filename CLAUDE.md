# slurm-agent

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
- **A key is required because something reads it, never because a list says so.** Which
  notification keys the laptop needs is derived from `config/notify.yaml`'s `channels` via
  `notify.CHANNEL_KEYS`, which the senders themselves read through. `manager.requires_env`
  is empty on purpose. A hand-kept list is wrong in both directions, and the quiet
  direction is the dangerous one: turn Slack on without updating it and `hc` passes while
  the escalation never arrives. A key with a default (`SMTP_PORT`) is reported, never
  failed.
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
- `docs/quickstart.py` goes one step further than `hc`: it puts a real interactive agent
  and a real batch agent on one allocation, each capped at `$1`. `hc` proves the wiring;
  the quick start proves the wiring carries an agent. It is the one notebook this repo
  commits with outputs, because those outputs are the proof a clone works.
- Which brief an agent gets is its own declared property (`prompt:` in `agents/*.yaml`,
  a template in `prompts/`). Every brief takes the same variables, which is what lets one
  launcher carry a twelve-hour experiment agent and a two-minute smoke agent.
- Source lives in `slurm_agent/` as jupytext `py:percent` paired notebooks with `if test():`
  blocks beside each function. Read the juplit skill (`poe skill`) before editing one.
- Everything that touches the cluster takes a `Runner` (see `slurm_agent/remote.py`). That
  single seam is why the suite needs no mocking library — tests pass a dict-backed fake.

## Remote paths use `$HOME`, never `~`

Commands are shell-quoted before they cross the ssh boundary, and `shlex.quote` quotes `~`
— so a tilde reaches the remote shell literally and never expands. Config files may be
written with `~` for readability, but anything interpolated into a remote command must be
resolved to `$HOME` first.
