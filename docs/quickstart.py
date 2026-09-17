# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -juplit
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Quick start: set this machine up, once
#
# Two commands, then you stop typing commands. Run this on the laptop the first time you
# set the repo up, and again whenever you doubt it.
#
# 1. `poe init` — create the local footprint and report on everything
# 2. `poe hc --full` — until it is green
# 3. talk to Claude
#
# Step 3 is the whole point. You do not bring up allocations, launch agents or poll them;
# the manager does, and it reports progress and spend back to you. **This notebook does not
# do it for you either** — it shows you the commands, and they are the real ones.
#
# ## Where this runs
#
# **On your laptop, in this checkout** — not on the login node. This repo is the control
# plane. What it needs is an ssh session to the login node that is **already
# authenticated**, because UW 2FA cannot be answered from a notebook:
#
# ```bash
# ssh tillicum-login      # answer 2FA, then leave this terminal alone
# ```
#
# The `ControlMaster` it holds is what every command below rides on. If something starts
# hanging, that session died — re-open it and re-run. Nothing here loses state, because
# nothing is *kept* here.
#
# ## Three places, and why the report keeps saying so
#
# Almost every confusing thing about setup comes from three machines each wanting a
# different `.envrc`, and a report that does not say which one it means.
#
# | Place | What lives there | What its `.envrc` holds |
# |---|---|---|
# | **This laptop** | this checkout, `~/.ssh/config`, the manager | nothing, in the shipped repo — `config/manager.yaml` declares no keys |
# | **The login node** | the run root, `tmux`, a Claude credential, a git credential | nothing — no `.envrc` here |
# | **Each staged repo on the cluster** | the repo one agent runs in | the keys **that agent** declares |
#
# Every row of the report below sits under a heading naming its machine, and that heading
# is the answer to *where do I fix this*. The staged-repo headings are also the list of
# repos this clone manages: there is no registry beyond `agents/*.yaml`.
#
# ## What it costs
#
# A few cents. `hc --full` makes one real Claude call per machine plus one per MCP server
# per machine, opens a one-minute allocation to prove it survives the ssh closing, and
# reads your Notion Tasks database. Nothing here runs an agent.

# %% [markdown]
# ### Run from the repo root
#
# The commands below are the real ones, so they need the real working directory.

# %%
import os
import pathlib

while not pathlib.Path("pyproject.toml").exists() and pathlib.Path.cwd() != pathlib.Path("/"):
    os.chdir("..")
print(pathlib.Path.cwd())

# %% [markdown]
# ## 1. `poe init`
#
# One report, grouped by machine, and that is the whole output. `init` creates what it can
# and says nothing itself; what exists afterwards is the report's to state, and what was
# just created rides along inside the row about that thing as a `·` note.
#
# It creates `.envrc` at mode 0600 and the ssh host entries on **this laptop**, and the run
# root on **the login node**. It never overwrites: an existing `.envrc` is kept, and a
# `tillicum-login` you defined yourself means the ssh block is skipped entirely.
#
# **Expect it to fail the first time.** The `.envrc` it just wrote is full of
# `<secret-here>`, and every failing row carries the `fix:` line for it.

# %%
# !uv run poe init

# %% [markdown]
# ## 2. Do what the report said, then re-run it
#
# Work the `MISSING` rows, by machine. Usually:
#
# ```bash
# $EDITOR .envrc     # replace every <secret-here> that is NOT commented out
# chmod 600 .envrc
#
# ssh tillicum-login       # then, on the login node:
#   claude                 #   log in once, interactively
#   gh auth login          #   or put a PAT in git's credential store
# ```
#
# Only the keys the report lists under `this laptop` are yours to fill in here — and in a
# fresh clone there are none, so `.envrc` is all comments and `my keys` is green with
# nothing to do. The manager reaches you by talking to you, so it holds no credential for
# that. Anything written **commented out** belongs to a staged repo on the cluster;
# filling it in here changes nothing.
#
# Claude itself is checked on both machines, and in two separate ways, because they fail
# separately. `claude auth` is free and instant and says only whether you are logged in.
# The `mcp …` rows are the ones people miss: the manager opens Notion task rows and reads
# GitHub through the servers in `.mcp.json`, each of which is its own OAuth grant that
# expires on its own schedule. Being logged in to Claude does not make Notion answer.
#
# There is a row per server per machine — `mcp notion` and `mcp github` here, and the same
# two on the login node for the agents, which authorise separately again. **Plain `poe hc`
# lists them as `SKIPPED`**, because proving one costs a cent; `--full` turns each into a
# real verdict. A `SKIPPED` row is never a pass, and that is the point: the fast tier still
# names the thing it has not proved. Each full-tier row runs a
# real headless `claude -p` that calls one read-only tool, because that is the only thing
# that proves a grant is live: `claude mcp list` reports a stored approval record, not what
# a session actually does. `mcp notion` also proves `data_source` in `config/tasks.yaml`
# names a database that exists. If you have used Notion and GitHub from Claude Code on a
# machine they are already authorised; if not, run `claude` there once and approve them.
#
# Then re-run until green. `hc` alone is the fast tier — seconds, no tokens, no GPU —
# and `--full` adds the slow proofs.

# %%
# !uv run poe hc --full

# %% [markdown]
# A `SKIPPED` row is never a pass — if the login node is unreachable, everything behind it
# is skipped rather than failed, so one broken link does not read as eight problems. A
# staged repo reading `not cloned yet` **is** fine: a workdir is created by the first
# launch, not by setup.

# %% [markdown]
# ## 3. From here on, you only talk to Claude
#
# Setup is done. `claude` started **in this repo root** comes up as the manager — no
# wrapper, no flags to remember — because of four files Claude Code reads by itself:
#
# | File | What it does |
# |---|---|
# | `CLAUDE.md` | Opens with *"if you are reading this, you are the manager"* |
# | `.claude/skills/slurm-orchestration/SKILL.md` | The procedure: tasks, sizing, launching, supervising, teardown |
# | `.mcp.json` | Notion and GitHub reachable |
# | `.claude/settings.json` | Those servers pre-enabled, `poe` pre-approved, `.envrc` denied |
#
# So the normal way to work is a terminal, in this directory:
#
# ```bash
# cd ~/src/slurm-agent && claude --remote-control "Tillicum manager"
# > Pick up TASK-118 on Tillicum. Keep me posted on progress and spend.
# ```
#
# **That is the whole interface.** A task id is enough: the manager reads the row in Notion,
# works out what the task asks for and which repo it is in, sizes the compute, writes the
# agent config, launches onto Tillicum, supervises, and reports back. You never name an
# allocation or a workdir.
#
# `--remote-control` is the flag worth typing every time, and it is why **this repo sends
# no email and has no Slack webhook**. The session also appears at claude.ai/code and in
# the Claude app, so a run you started at your desk is one you can read on the couch and
# steer from there. Claude keeps running on *this laptop* — which matters, because it is
# the only machine on a network that reaches Tillicum.
#
# Three things it does not do:
#
# * **It is interactive-only.** A `claude -p` prints and exits, so the pasted blocks below
#   stay invisible to the console. This is the reason to work as a conversation.
# * **It does not follow the agents.** They run headless on a compute node; ask the manager
#   about them — it is the one supervising them, and it reads their notebooks in place.
# * **The repo cannot turn it on for you.** Claude Code ignores `remoteControlAtStartup:
#   true` in a checked-in `.claude/settings.json` on purpose, so a clone can never put
#   someone's session in someone else's account. Set it in **your** `~/.claude/settings.json`
#   to have every session do it, or type the flag.
#
# It needs a Pro, Max, Team or Enterprise login (not an API key), and `ANTHROPIC_BASE_URL`
# unset or pointing at `api.anthropic.com`.
#
# ### Nothing pushes to you, and nothing needs to
#
# There is no notifier here, and `.envrc` holds no SMTP or Slack key, because a second
# channel would mean a supervision decision could arrive by email while the session that
# made it said nothing. The manager is the channel:
#
# * A threshold firing comes back as a `NEEDS YOU` line out of `poe agent-watch`, which the
#   manager reads and tells you about.
# * Cost accumulated while nobody was looking is in `poe spend`, which reads a ledger the
#   scheduled poll appends to **on the cluster** — so it survives a closed laptop and two
#   sessions agree about it. `poe monitor-install` puts that poll on a schedule.
#
# Ask "what has this cost so far" and the manager answers from both.

# %% [markdown]
# ### The same thing from here, as bash you can paste
#
# `claude -p` is that manager, printing its reply and exiting. `--output-format` defaults to
# `text`, so stdout is the reply — which is all `$( )` needs to feed one session into the
# next.
#
# The cell below is one `%%bash` block: **copy it into a terminal and it runs unchanged.**
# Nothing to fill in.
#
# Its first half opens two tasks, only so this walkthrough has something to point at — in
# real use the rows already exist (you wrote a spec, or a sync-up filed them) and you delete
# that half and put your own ids in `IDS`.

# %% language="bash"
# IDS=$(claude -p 'Open two small tasks in our Notion Tasks database, for work in this repo:
# (1) add a docstring example to slurm_agent/remote.py
# (2) add a line to README.md describing poe status
# Reply with the two task ids, one per line, and nothing else.')
#
# echo "opened: $IDS"
#
# claude -p "Pick up these tasks on Tillicum: $IDS
#
# Read each one in Notion to see what it asks for. Size the compute yourself.
# Tell me what each agent produced and what it cost."

# %% [markdown]
# From those two sentences the manager will: run `poe hc --full`, decide the compute (two
# small tasks belong as two steps on **one** allocation — Tillicum permits one interactive
# allocation, so a second job is not a tidier answer, it is an unavailable one), write an
# agent config for each, launch them onto the cluster, watch them, and drop the allocation
# when the last one is done.
#
# Ask for an update whenever you want one. This needs no ids: the manager re-derives
# everything from `squeue`, `sacct` and the run roots on the cluster, because the laptop
# holds nothing it cannot rebuild.

# %% language="bash"
# claude -p 'How are my Tillicum runs going, and what have they cost so far?'

# %% [markdown]
# For a long run you would rather watch than poll, drop the `-p` and talk to it — which is
# also the only form that reaches your phone:
#
# ```bash
# cd ~/src/slurm-agent && claude --remote-control "Tillicum manager"
# > Pick up TASK-118 on Tillicum. Keep me posted on progress and spend.
# ```
#
# When the work is done the durable record is the Notion row — each agent writes its own
# findings there — and the allocation is gone, because the manager drops it. Nothing is left
# running and nothing is left to clean up by hand.

# %% [markdown]
# ## What this proved
#
# Every line is a row in the report above, so you never have to take this table's word
# for it.
#
# | Proved | The row |
# |---|---|
# | The local footprint is real | `.envrc` at 0600 and `ssh config`, each with a `·` note saying what `init` did |
# | ssh reaches the login node | `reachable` |
# | Every key this laptop reads is filled | `my keys` — which in a fresh clone demands none |
# | Git can reach the repo from both machines | the `github …` rows, one per heading |
# | It can **push**, not just read | `github … push`, from `hc --full`'s `--dry-run` probe |
# | Claude is logged in on **both** machines | `claude auth`, under each heading — free, so it is in the fast tier |
# | Claude works headlessly there, and costs something | `agent credential`, under each heading — `hc --full` asserts `total_cost_usd > 0` |
# | Every MCP server is authorised, on **both** machines | one `mcp …` row per server per heading — named in every tier, proved by a real tool call under `--full` |
# | The manager can reach the Notion Tasks database | `mcp notion` — which is how a run gets a task id |
# | An allocation outlives the ssh that asked for it | `allocation probe`, in the mode you configured |
# | This clone knows which repos it manages | one `staged repo ·` heading per `agents/<kind>.yaml` |
# | `tmux` is there to hold allocations | `tmux`, when `allocation_mode` is the default |
# | The run root exists on the cluster | `run root` |
#
# That is everything an agent needs in order to start and to finish. It proves nothing about
# an agent itself — whether a supervision threshold fires, whether a task's own code works.
# Those are answered by running real work, which is the manager's job.
#
# A cluster-side `.envrc` is also unproven, because no shipped agent declares a key. The
# first agent of yours that does is the first time those rows say anything — and `clone` and
# `worktree` rows only say something once a workdir exists.
#
# ## When something fails
#
# Every row says which machine it is about, so *where do I fix this* is answered by the
# heading it sits under.
#
# | Symptom | Where to look |
# |---|---|
# | A command hangs | The authenticated ssh session died. Re-open it; re-run. |
# | `reachable` MISSING, `not authenticated` | Open a terminal, `ssh tillicum-login`, answer 2FA, leave it open. |
# | Everything behind it `SKIPPED` | That is the point: one broken link, not eight problems. A skip is never a pass. |
# | `my keys` MISSING | Fill them in *this laptop's* `.envrc`. It only asks for what `config/manager.yaml` declares. |
# | `claude auth` MISSING | Run `claude auth login` on that machine. |
# | `agent credential` MISSING | Logged in, but the headless call failed or reported no cost. Run `claude` there once. |
# | `mcp …` SKIPPED | You ran plain `poe hc`. The fast tier names the servers; `--full` proves them. |
# | `mcp servers` MISSING | `.mcp.json` or `config/mcp.json` is gone or empty. Restore it from the repo. |
# | `mcp github` MISSING | Run `claude` on that machine and authorise the GitHub MCP server. |
# | `mcp notion` MISSING | Same, for Notion — or `data_source` in `config/tasks.yaml` names nothing real. |
# | `github …` MISSING | That machine has no git credential for the repo. `gh auth login` there, or a PAT. |
# | `github … push` MISSING | It can read but not write. The credential needs the repo scope. |
# | `clone` says `not cloned yet` | Not a fault. A workdir is created by the first launch. |
# | `worktree` MISSING | Something is uncommitted in the staged repo, and a launch will refuse it. |
# | `hc` is green but a launch fails | Tell the manager what it said. `hc` proves the machines, not an agent's own code. |
