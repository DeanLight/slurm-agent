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

## Working here

- `poe --help` is the inventory. Every capability is a `poe` task wrapping one
  `slurm-agent` command, so an agent driving this repo uses the same surface a human does.
- `poe init` **creates** the local footprint; `poe healthcheck` (alias `poe hc`) **verifies**
  it and creates nothing. `hc` is fast on purpose — run it after moving network or
  re-authing to Tillicum, where a dropped `ControlMaster` is the usual culprit.
- Source lives in `slurm_agent/` as jupytext `py:percent` paired notebooks with `if test():`
  blocks beside each function. Read the juplit skill (`poe skill`) before editing one.
- Everything that touches the cluster takes a `Runner` (see `slurm_agent/remote.py`). That
  single seam is why the suite needs no mocking library — tests pass a dict-backed fake.

## Remote paths use `$HOME`, never `~`

Commands are shell-quoted before they cross the ssh boundary, and `shlex.quote` quotes `~`
— so a tilde reaches the remote shell literally and never expands. Config files may be
written with `~` for readability, but anything interpolated into a remote command must be
resolved to `$HOME` first.
