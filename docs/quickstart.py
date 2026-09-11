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
# # Quick start: prove a fresh clone really works
#
# Run this once, on the laptop, the first time you set this repo up on a machine — and
# again whenever you doubt it. It walks the whole path in order:
#
# 1. `poe init` — create the local footprint
# 2. fill `.envrc`, then `poe hc --full --send` — prove every wire carries current
# 3. one allocation, and **two** agents on it: one **interactive**, one **batch**
# 4. watch both from here: progress, spend, and the supervisor's decisions
# 5. both push to the **same throwaway PR**, which you look at and never merge
# 6. tear the allocation down
#
# The two agents are the point. `poe hc` proves the wiring; this proves the wiring
# *carries an agent*, in both of the modes real work uses, for about a dollar.
#
# ## Where this runs
#
# **On your laptop, in this checkout** — not on the login node. This repo is the control
# plane: it holds your `.envrc`, edits *your* `~/.ssh/config`, and sends notifications from
# here. Every cell below reaches Tillicum over ssh; none of them run there.
#
# What it does need is an **ssh session to the login node that is already authenticated**,
# because UW 2FA cannot be answered from a notebook. Open one in a terminal and leave it
# open — the `ControlMaster` it holds is what every cell below rides on:
#
# ```bash
# ssh tillicum-login      # answer 2FA, then leave this terminal alone
# ```
#
# If a cell suddenly starts timing out halfway through, that session died. Re-open it and
# re-run the cell; nothing here loses state, because none of it is *kept* here.
#
# ## Three places, and why the reports keep saying so
#
# Almost every confusing thing about setting this up comes from three different machines
# each wanting a different `.envrc`, and a report that does not say which one it means.
# There are exactly three kinds of place:
#
# | Place | What lives there | What its `.envrc` holds |
# |---|---|---|
# | **This laptop** | this checkout, `~/.ssh/config`, the supervision loop | the keys for **reaching you** — SMTP, Slack |
# | **The login node** | the run root, `tmux`, your Claude credential | nothing — no `.envrc` here at all |
# | **Each staged repo on the cluster** | the repo an agent runs in | the keys **that agent** declares, e.g. `HF_TOKEN` |
#
# So `HF_TOKEN` is never a laptop problem. `poe init` writes your laptop's `.envrc` with
# keys like that **commented out**, naming the remote path each belongs in instead, and
# `poe hc` checks each key against the machine that actually reads it. Every row in every
# report below sits under a heading naming its machine.
#
# ### How it knows which repos it manages
#
# It reads `agents/*.yaml`, one file per agent, and nothing else. Each file names a repo,
# a ref and the workdir it is staged into — and that is the entire list. There is no
# registry, nothing remembered between runs, and nothing to deregister: delete the file
# and it stops managing that repo. `poe init` prints the list before it does anything, and
# the next cell prints it on its own.
#
# **Every agent shipped here points at this repo**, on purpose. A fork should be able to
# run its whole sanity check without access to anything else, and the one repo a fork can
# always clone and push to is itself. `agents/experiment-runner.yaml` is a placeholder in
# exactly that sense — repoint its `repo`, `ref` and `workdir` at your experiment repo
# when you have one. The two smoke agents are meant to stay pointed here.
#
# ## What it will cost
#
# One GPU for well under an hour, and two agents capped at `$1` each by
# `agents/smoke.yaml`. The cap is a runaway guard on list-priced tokens, not a bill — under
# a subscription the real limit is your plan's usage window.

# %% [markdown]
# ## 0. The harness
#
# One helper, because every step below is "run a `poe` task and read what it says". It
# never raises: a failing check is something to *read*, and `poe init` failing the first
# time is the expected path, not an accident.

# %%
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path


def _root() -> Path:
    """The repo root, wherever the kernel happened to start."""
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "pyproject.toml").exists() and (d / "slurm_agent").is_dir():
            return d
    raise RuntimeError("run this notebook from inside the slurm-agent checkout")


ROOT = _root()
os.chdir(ROOT)


def sh(cmd: str, *, timeout: int = 900, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command at the repo root. Prints what happened; never raises."""
    print(f"$ {cmd}\n")
    try:
        # COLUMNS, because `poe` output is captured rather than attached to a terminal:
        # rich would otherwise fall back to 80 and fold the report's tables.
        p = subprocess.run(cmd, shell=True, cwd=ROOT, timeout=timeout,
                           capture_output=True, text=True,
                           env={**os.environ, "COLUMNS": "110"})
    except subprocess.TimeoutExpired:
        print(f"!! timed out after {timeout}s — is the ssh session to the login node "
              "still open?")
        raise
    if not quiet:
        print((p.stdout or "") + (p.stderr or ""), end="")
    print(f"\n[exit {p.returncode}]")
    return p


print(f"repo root: {ROOT}")

# %% [markdown]
# ## 0b. What this clone manages
#
# Read this before running anything. It is the whole inventory: your laptop, the login
# node, and one staged repo per `agents/<kind>.yaml`. Every `MISSING` row you see later
# belongs to exactly one of these lines.

# %%
from slurm_agent import preflight  # noqa: E402
from slurm_agent.config import ClusterConfig, ManagerConfig, load, load_agents  # noqa: E402

cluster = load("config/cluster.yaml", ClusterConfig)
manager = load("config/manager.yaml", ManagerConfig)
agent_configs = load_agents()
preflight.print_inventory(cluster, manager, agent_configs)

# %% [markdown]
# ## 1. `poe init` — create the local footprint
#
# It prints the inventory again, then a **Creating:** block and a **Checking:** block, both
# grouped by machine.
#
# What it creates, and where:
#
# * **on this laptop** — `.envrc` at mode 0600, and the ssh host entries appended to
#   `~/.ssh/config` between markers;
# * **on the login node** — the run root.
#
# It never overwrites. An existing `.envrc` is kept as-is. A `tillicum-login` you defined
# yourself means the ssh block is skipped entirely — your other clusters and servers are
# not touched, and nothing there is replaced.
#
# The `.envrc` it writes is **not** a copy of `templates/envrc.example`. It gets a header
# written for that file — what it is, that it is read on this laptop only, which keys it
# holds — and any key that is only read on the cluster is written **commented out**, with
# the remote path it belongs in beside it. Filling one of those in here changes nothing.
#
# Then it runs a **full** healthcheck, which really sends mail and Slack.
#
# **Expect this to fail the first time**, and read the failure rather than fixing it
# blind — the `.envrc` it just wrote is full of `<secret-here>`.

# %%
sh("uv run poe init")

# %% [markdown]
# ## 2. Fill in **this laptop's** `.envrc`
#
# Only the keys under `this laptop` in the report — the ones `config/manager.yaml`
# declares, for reaching you. The commented-out ones are not yours to fill in here; they
# belong to a staged repo, and step 2b is where those go.
#
# `.envrc` is gitignored and holds the real values; the YAML names **keys** and nothing
# committed here ever holds a value.
#
# Edit it in a terminal — not from this notebook, which would put secrets in an output
# cell:
#
# ```bash
# $EDITOR .envrc     # replace every <secret-here> that is NOT commented out
# chmod 600 .envrc
# ```
#
# For email you want an **app password**, not your account password. For Slack you want an
# [incoming webhook](https://api.slack.com/messaging/webhooks) URL.
#
# The fast healthcheck below says which keys are still placeholders, under the heading of
# the machine each is read on. It creates nothing, prints names only, and reads values from
# `.envrc` the same way every `poe` task does — via `[tool.poe] envfile`, so this repo has
# no credentials reader of its own and nothing to leak into an output cell.

# %%
sh("uv run poe hc")

# %% [markdown]
# ## 2b. The other `.envrc`s — one per staged repo, on the cluster
#
# A different file, on a different machine, holding different keys. It lives beside the
# repo an agent runs in, and it holds exactly what that agent's `requires_env` declares —
# `HF_TOKEN` and friends, plus the notification keys if you want that agent to reach you
# from the compute node.
#
# The report names the exact path in its heading, so there is nothing to work out. For
# `agents/experiment-runner.yaml` that is:
#
# ```bash
# scp templates/envrc.example tillicum-login:~/work/deepreasoner-baselines/.envrc
# ssh tillicum-login 'chmod 600 ~/work/deepreasoner-baselines/.envrc'
# ssh tillicum-login    # then $EDITOR it there
# ```
#
# `poe hc` checks that file's **mode** too, and fails loudly at 0644 — Tillicum's
# filesystem is shared, and a group-readable app password is the real exposure here.
#
# **Both smoke agents declare no keys at all**, on purpose: a sanity check that needs a
# credential has two extra ways to fail. So you can skip this step for the trial runs
# below and come back to it before launching a real experiment agent — their rows will
# read `declares no keys — nothing needed here`.

# %% [markdown]
# ### And Claude, logged in on the cluster
#
# Remote agents run under your subscription, authenticated on Tillicum once:
#
# ```bash
# ssh tillicum-login
# claude               # log in interactively, then exit
# ```
#
# `hc --full` proves it works headlessly **and** reports a non-zero cost. A subscription
# reporting zero would silently disarm every `--max-budget-usd` in this repo, and nothing
# else would notice.

# %% [markdown]
# ## 3. `poe hc --full --send` — every wire carries current
#
# `hc` alone is the fast one: run it after moving network or re-authing, when a dropped
# `ControlMaster` is the usual culprit. `--full` adds the slow proofs — a real allocation
# probe and a real headless Claude call. `--send` really delivers a test message from your
# laptop *and* from Tillicum, which are two different egress paths and neither stands in
# for the other.
#
# Read it by group. `MISSING` under `this laptop` is something you fix here; under a
# `staged repo · …` heading it is something you fix over ssh, at the path in the heading.
# A `SKIPPED` row is never a pass — if the login node is unreachable, everything behind it
# is skipped rather than failed, so that one broken link does not read as eight problems.
#
# Everything below this point spends money.

# %%
hc = sh("uv run poe hc --full --send", timeout=1800)
assert hc.returncode == 0, "fix the MISSING rows above before spending a GPU-hour"

# %% [markdown]
# ## 4. The throwaway branch both agents will push to
#
# The trial runs are real launches, so they end the way real launches end: a commit,
# pushed. Both push to **one** branch, so one PR shows both halves side by side.
#
# `agents/smoke.yaml` already names that branch. This cell creates it on the target repo
# from its default branch, if it is not there yet — a shallow clone in a temp directory, so
# nothing lands in this checkout.

# %%
smoke = agent_configs["smoke"]
smoke_batch = agent_configs["smoke-batch"]
SMOKE_URL = f"https://github.com/{smoke.repo}"

# One branch, two staged trees. Sharing the branch is the point — it is what makes both
# halves land in one PR. Sharing a *tree* would not work: `stage()` refuses to launch onto
# a dirty one, so the second half would be refused while the first still had an
# uncommitted notebook.
assert smoke.ref == smoke_batch.ref
assert smoke.workdir != smoke_batch.workdir
print(f"target: {smoke.repo} · branch: {smoke.ref}")
print(f"interactive: {smoke.workdir}  (${smoke.max_budget_usd} cap)")
print(f"batch:       {smoke_batch.workdir}  (${smoke_batch.max_budget_usd} cap)")

# %%
heads = sh(f"git ls-remote --heads {SMOKE_URL} {smoke.ref}", quiet=True)
if heads.stdout.strip():
    print(f"{smoke.ref} already exists — reusing it")
else:
    sh(textwrap.dedent(f"""
        tmp=$(mktemp -d) && git clone --depth 1 {SMOKE_URL} "$tmp" \\
          && git -C "$tmp" push origin HEAD:refs/heads/{smoke.ref} \\
          && rm -rf "$tmp"
    """).strip())

# %% [markdown]
# Open the PR now, so both agents push into something you can watch fill up. `gh` is the
# quick way; if you do not have it, click the URL this cell prints.
#
# **Do not merge it.** It is a sanity check, and its whole content is two notebooks saying
# which GPU answered.

# %%
if shutil.which("gh"):
    sh(f"gh pr create --repo {smoke.repo} --head {smoke.ref} --draft "
       f'--title "Smoke: slurm-agent end-to-end sanity check (do not merge)" '
       f'--body "Two trial agents — one interactive, one batch — launched by '
       f'slurm-agent. Evidence only. Close this without merging."')
    sh(f"gh pr view --repo {smoke.repo} {smoke.ref} --json url,number,commits")
else:
    print(f"open it here: {SMOKE_URL}/compare/{smoke.ref}?expand=1")

# %% [markdown]
# ### One precondition this repo cannot check for you
#
# The agents push **from the compute node**, so the cluster needs its own credential for
# that repo — `gh auth login` on the login node, or a PAT in git's credential store.
# Nothing above proves that; the read below only proves the repo is reachable.
#
# If the credential is missing, you will not silently lose the run: step 4 of the smoke
# brief tells the agent to report `needs_human` on a rejected push rather than work around
# it, and `poe agent-status` will show it waiting.

# %%
sh(f"ssh tillicum-login 'git ls-remote --heads {SMOKE_URL} | head -3'")

# %% [markdown]
# ## 5. One allocation
#
# `job-up` is idempotent: run it twice and you get the same job back, not a second one.
# Tillicum permits **one** interactive allocation, which is exactly why the next two agents
# share this one as `--overlap` steps.
#
# Held in `tmux` on the login node by default, so you can attach to it and watch — the
# `attach:` line it prints is the command.

# %%
sh("uv run poe job-up smoke --gpus 1 --time 01:00:00")

# %% [markdown]
# ## 6. The interactive agent
#
# This is the mode you use when you are at the desk: the agent holds a step on the shared
# allocation, works in leases, and the supervisor renews or kills it.
#
# `--exp-id` gives it its own log directory, so the two halves cannot collide on one
# notebook. Launch refuses before anything expensive if the staged tree is dirty or a
# declared key is unfilled.

# %%
run_i = sh("uv run poe agent-run SMOKE-interactive --job smoke --agent smoke "
           "--exp-id smoke-interactive")

# %% [markdown]
# ## 7. Watch it, from here
#
# Three different questions, three commands:
#
# * `agent-status` — what each agent says it is doing, and what it has cost
# * `agent-logs --cells` — the notebook's cells, summarised **on the login node**, so a
#   4 MB notebook costs a few hundred tokens and is never copied to the laptop
# * `agent-watch --once` — one supervision pass: poll, decide, act, log
#
# Two independent progress signals sit behind these: what the agent *says* (the status
# block its hooks keep) and what is *observed* (the notebook's mtime). A run that stops
# saying anything and stops writing is stuck; a run that only stops saying things is not.

# %%
for _ in range(10):
    p = sh("uv run poe agent-status", quiet=True)
    print(p.stdout.strip() or "no remote agents")
    if "round 3/3" in p.stdout:
        print("\ninteractive half reported done")
        break
    time.sleep(30)

# %%
session_i = run_i.stdout.split("session ")[-1].strip().split()[0] if "session " in run_i.stdout else ""
print("session:", session_i or "(read it from the launch output above)")
if session_i:
    sh(f"uv run poe agent-logs {session_i} --cells")

# %% [markdown]
# ## 8. The batch agent
#
# The other mode: no shared allocation, no lease renewals, no laptop. `sbatch` queues it,
# it brings up its own GPU, runs, and **ends itself** when the agent exits — which is what
# makes it the right answer for overnight work and for anything you want to run in
# parallel with an interactive session.
#
# Same brief, same budget, same branch. Only two things differ, and both are declared in
# `agents/smoke-batch.yaml` rather than passed here: `mode: batch`, and its own workdir, so
# the two halves are not fighting over one staged checkout. How it gets a GPU is the whole
# thing this cell is here to prove.

# %%
run_b = sh("uv run poe agent-batch SMOKE-batch --agent smoke-batch --time 00:30:00 "
           "--exp-id smoke-batch")

# %% [markdown]
# It will sit `PENDING` in the queue for a while. That is the mode working, not failing.

# %%
for _ in range(20):
    p = sh("uv run poe status", quiet=True)
    print(p.stdout.strip())
    # `status` groups by section; the batch half is done when its row lands under
    # `completed`, which is the one place SLURM's own COMPLETED shows up next to the task.
    if any("SMOKE-batch" in ln and "COMPLETED" in ln for ln in p.stdout.splitlines()):
        print("\nbatch half completed")
        break
    time.sleep(60)

# %% [markdown]
# ## 9. What the manager saw
#
# `status` is the whole picture — running, queued, completed, failed — and every line of it
# is derived from `squeue`, `sacct` and the run roots on the cluster. Nothing is cached
# here. Close this laptop mid-run, re-open it tomorrow, and the same command tells you the
# same story, because the story was never stored here to be lost.
#
# The spend columns are the two costs kept apart on purpose: **GPU-hours**, which are real
# money on a real account, and **agent tokens**, priced at API list rates by the CLI even
# when you are on a subscription. Do not add them up and do not reconcile either against an
# invoice.

# %%
sh("uv run poe status")

# %%
sh("uv run poe agent-watch --once")

# %% [markdown]
# ## 10. The PR
#
# Two commits, from two launch modes, on one branch: one notebook per agent, each carrying
# the `nvidia-smi` output of the GPU that actually answered. That is the artifact this
# whole exercise exists to produce.

# %%
sh(f"git ls-remote {SMOKE_URL} refs/heads/{smoke.ref}")
if shutil.which("gh"):
    sh(f"gh pr view --repo {smoke.repo} {smoke.ref} --json url,number,commits,files")
else:
    print(f"look here: {SMOKE_URL}/compare/{smoke.ref}?expand=1")

# %% [markdown]
# **Close it without merging.** The smoke agents stage **this repo** — the one repo a fork
# is always allowed to clone and push to, which is why the sanity check targets it rather
# than something of mine you might not have access to. Merging would commit two throwaway
# notebooks into your own control plane.

# %% [markdown]
# ## 11. Tear down
#
# The allocation is the only thing here that costs money while you are not looking. Drop
# it first.
#
# `flush` prunes the finished runs' roots on the cluster — which is what makes them
# disappear from `status`, because `status` reads those roots rather than a local list.
# Failed runs are kept unless you ask: their `agent.err` is the only record of why they
# died.

# %%
sh("uv run poe job-down smoke")

# %%
sh("uv run poe flush --older-than 0d --dry-run")

# %% [markdown]
# Re-run that without `--dry-run` once you have read what it would remove.

# %% [markdown]
# ## What this proved, and what it did not
#
# | Proved | How |
# |---|---|
# | The local footprint is real | `poe init` created `.envrc` at 0600 and appended ssh hosts without clobbering |
# | Every declared key is filled | `poe hc` reads names from YAML, values from `.envrc` |
# | You can be reached | `--send` really delivered, from the laptop **and** from Tillicum |
# | Claude works headlessly there, and costs something | `hc --full` asserts `total_cost_usd > 0` |
# | An allocation comes up and is attachable | `job-up`, and the `attach:` line it printed |
# | Staging refuses to launch onto a dirty tree | `agent-run` preflighted before spending |
# | The interactive path works end to end | `SMOKE-interactive` pushed a notebook |
# | The batch path works end to end | `SMOKE-batch` queued, ran, and ended itself |
# | Progress and spend are visible from here | `agent-status`, `agent-logs --cells`, `status` |
# | The supervisor decides and records | `agent-watch --once` |
#
# It did **not** prove that a kill or a lease renewal works — the smoke agents finish in
# two minutes, long before any threshold fires — nor that a real experiment agent's
# `requires_env` are present on the cluster, since the smoke agent declares none.
#
# ## When something fails
#
# | Symptom | Where to look |
# |---|---|
# | A cell hangs, then times out | The authenticated ssh session died. Re-open it; re-run. |
# | `hc` shows `SKIPPED` rows | The cluster was unreachable. A skip is never a pass. |
# | `notify send (tillicum)` MISSING | The cluster-side `.envrc` — mode and keys, not the laptop's. |
# | `agent credential` MISSING | `ssh tillicum-login` and run `claude` once, interactively. |
# | Launch refuses: dirty workdir | Something wrote inside the staged repo. Nothing this repo writes should. |
# | Agent stuck at `needs_human` | `poe agent-status` names what it is waiting on — usually the cluster's git credential. |
# | Batch job never leaves `PENDING` | `poe status`. The queue is the queue; that is not a failure. |
