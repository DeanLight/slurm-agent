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
# 1. `poe init` — create the local footprint, and get one report of everything
# 2. fill `.envrc`, then `poe hc --full --send` — prove every wire carries current, on
#    **both** machines: Claude authenticated, git authenticated, and able to push
# 3. **one** allocation, and two real development tasks running on it as two steps
# 4. watch both from here: progress, spend, and the supervisor's decisions
# 5. read what each produced, in place on the cluster
# 6. tear the allocation down
#
# The two agents are the point. `poe hc` proves the wiring; this proves the wiring
# *carries an agent*, for about a dollar.
#
# **Nothing is pushed and there is no pull request.** Each task writes its notebook under
# its own run root on the cluster, and you read it with `poe agent-logs`. Step 2 already
# proved push, with a `--dry-run` from each machine, so a trial run has nothing left to
# prove about git — and a PR you would only close is friction with no payoff.
#
# Once you have run this once, you do not run it again by hand. Open Claude Code in this
# repo and say *"run these two tasks on Tillicum"*: `.claude/skills/slurm-orchestration/SKILL.md`
# is these same five steps written for the manager, including how it decides whether tasks
# share one allocation or need jobs of their own.
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
# | **This laptop** | this checkout, `~/.ssh/config`, the supervision loop | the keys for **reaching you** — whatever `config/notify.yaml`'s channels need, SMTP by default |
# | **The login node** | the run root, `tmux`, your Claude credential, its own git credential | nothing — no `.envrc` here at all |
# | **Each staged repo on the cluster** | the repo one agent runs in | the keys **that agent** declares, e.g. `HF_TOKEN` |
#
# So a key like `HF_TOKEN` is never a laptop problem. `poe hc` checks each key against the
# machine that actually reads it, and every row of every report below sits under a heading
# naming its machine.
#
# None of the agents shipped here declare any keys, so out of the box that third column is
# empty and there is nothing to do on the cluster. When one of yours does declare a key,
# `poe init` writes it into your laptop's `.envrc` **commented out**, with the remote path
# it belongs in beside it — filling it in here would change nothing.
#
# ### How it knows which repos it manages
#
# It reads `agents/*.yaml`, one file per agent, and nothing else. Each file names a repo,
# a ref and the workdir it is staged into — and that is the entire list. There is no
# registry, nothing remembered between runs, and nothing to deregister: delete the file
# and it stops managing that repo. You never have to ask separately: every report below
# heads one group per agent, so the list is wherever the answer is needed.
#
# **Every agent shipped here points at this repo**, on purpose: a fork should be able to
# run its whole sanity check without access to anything else, and the one repo a fork can
# always clone is itself. `agents/experiment-runner.yaml` is a placeholder in exactly that
# sense — repoint its `repo`, `ref` and `workdir` at your experiment repo when you have one.
# The two trial tasks are meant to stay pointed here; they only ever *read* what they stage.
#
# ## What it will cost
#
# One GPU for well under an hour, and two agents capped at `$1` each by
# `agents/smoke.yaml` and `agents/smoke-2.yaml`. The cap is a runaway guard on list-priced
# tokens, not a bill — under a subscription the real limit is your plan's usage window.
#
# Both tasks declare `gpus: 0`, so they cost the allocation's time rather than a device
# each. The allocation is sized for whatever *does* claim a GPU — here, nothing.

# %% [markdown]
# ## 0. The harness
#
# One helper, because every step below is "run a `poe` task and read what it says". It
# never raises: a failing check is something to *read*, and `poe init` failing the first
# time is the expected path, not an accident.

# %%
import os
import subprocess
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

# %%
# The agent configs, keyed by kind. This IS the list of repos this clone manages — there is
# no registry behind it, and later cells read the trial tasks' settings straight out of it
# rather than repeating them.
from slurm_agent.config import load_agents  # noqa: E402

agent_configs = load_agents()
for kind, cfg in agent_configs.items():
    print(f"agents/{kind}.yaml  {cfg.repo}@{cfg.ref}  ->  {cfg.workdir}")

# %% [markdown]
# ## 1. `poe init` — create the local footprint
#
# One report, grouped by machine, and that is the whole output. `init` creates what it can
# and then says nothing itself; what exists afterwards is the report's to state, and what
# it just created rides along inside the row about that thing as a `·` note. So a file it
# could not create is not announced twice — once in ssh's words and once, correctly, as the
# row that says the login node is unreachable.
#
# There is no separate inventory either: the headings are the inventory — one per place,
# one per `agents/<kind>.yaml` naming its repo, ref and workdir.
#
# What it creates, and where:
#
# * **on this laptop** — `.envrc` at mode 0600, and the ssh host entries appended to
#   `~/.ssh/config` between markers (the `.envrc` and `ssh config` rows);
# * **on the login node** — the run root every agent's files live under (the `run root`
#   row). If it cannot reach the login node it says nothing about it: the `run root` row is
#   about to say the same thing, better.
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
# Then it checks everything — the **full** tier, which really sends a message on every
# channel `config/notify.yaml` turns on, from both machines.
#
# **Expect this to fail the first time**, and read the failure rather than fixing it
# blind — the `.envrc` it just wrote is full of `<secret-here>`.

# %%
sh("uv run poe init")

# %% [markdown]
# ## 2. Fill in **this laptop's** `.envrc`
#
# Only the keys the report lists under `this laptop` — the ones the channels you turned on
# need, for reaching you. Anything written **commented out** is not yours to fill in here:
# it belongs to a staged repo on the cluster, and §2b is where those go. Out of the box
# there are none, because no shipped agent declares a key.
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
# **Only the channels you turned on need keys.** `config/notify.yaml`'s `channels` decides,
# and it ships as `[email]` — so Slack's webhook is not required until you add `slack` to
# that list. That is what optional means here: a channel that is on but cannot send is
# worse than one that is off, because you only find out when nothing arrives.
# `SLURM_AGENT_SMTP_PORT` has a default of 587, so it is reported as defaulted, never
# failed.
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
# **Nothing to do here today — skip to §3.** Every agent shipped with this repo declares
# `requires_env: []`, so all three staged-repo rows read `declares no keys — nothing needed
# here`. That is deliberate twice over: a sanity check that needs a credential has two
# extra ways to fail, and an example that demands a token turns a correctly-set-up laptop
# red over a file its owner has no reason to have created.
#
# It matters the moment one of *your* agents declares a key. Then it needs a different
# file, on a different machine, holding different keys: one beside the repo that agent runs
# in, containing exactly what its `requires_env` names — plus the notification keys if you
# want that agent to reach you from the compute node.
#
# You never work the path out. The report prints it in the heading above the row, and the
# row's `fix:` is the command. It looks like this:
#
# ```bash
# scp templates/envrc.example tillicum-login:<the workdir in the heading>/.envrc
# ssh tillicum-login 'chmod 600 <that path>'
# ssh tillicum-login    # then $EDITOR it there
# ```
#
# `poe hc` checks that file's **mode** too, and fails loudly at 0644 — Tillicum's
# filesystem is shared, and a group-readable app password is the real exposure here.

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
# `ControlMaster` is the usual culprit. `--full` adds the three slow proofs — a real
# allocation that must outlive the ssh that asked for it, a real headless Claude call that
# must report a non-zero cost, and a `--dry-run` push from each machine that proves **write**
# access rather than just read. `--send` really delivers a test message from your laptop
# *and* from Tillicum, which are two different egress paths and neither stands in for the
# other.
#
# Read it by group. `MISSING` under `this laptop` is something you fix here; under a
# `staged repo · …` heading it is something you fix over ssh, at the path in the heading.
# A staged repo reading `not cloned yet` is **not** a problem — a workdir is created by the
# first launch, not by setup.
# A `SKIPPED` row is never a pass — if the login node is unreachable, everything behind it
# is skipped rather than failed, so that one broken link does not read as eight problems.
#
# Everything below this point spends money.

# %%
hc = sh("uv run poe hc --full --send", timeout=1800)
assert hc.returncode == 0, "fix the MISSING rows above before spending a GPU-hour"

# %% [markdown]
# ## 4. The two trial tasks
#
# Two small development tasks, run by two real agents. Neither touches git: each writes one
# notebook under **its own run root** on the cluster, carrying the `nvidia-smi` output of
# the GPU that answered. There is no branch, no PR and nothing to merge — you read the
# result with `poe agent-logs`, and `poe flush` removes it when you are done.
#
# That is on purpose twice over. Nothing this repo writes may land inside a staged repo,
# because a dirty tree blocks the next launch. And needing to review a pull request to see
# that a sanity check worked is friction with no payoff: `hc --full` already proved push on
# both machines with a `--dry-run`, so the trial has nothing left to prove about git.
#
# Both declare `gpus: 0` — they read one line out of `nvidia-smi` and write a file — which
# is what lets them share **one** allocation as two steps, on a cluster that permits one
# interactive allocation at a time.

# %%
task_a = agent_configs["smoke"]
task_b = agent_configs["smoke-2"]

# Two tasks at once need two agent configs: each stages into its own workdir, and two
# launches racing in one checkout is the failure that looks like a cluster problem.
assert task_a.workdir != task_b.workdir
assert (task_a.gpus, task_b.gpus) == (0, 0)
for name, cfg in (("task A", task_a), ("task B", task_b)):
    print(f"{name}: agents/{'smoke' if cfg is task_a else 'smoke-2'}.yaml · "
          f"{cfg.gpus} gpu · ${cfg.max_budget_usd} cap · notebook under its run root")

# %% [markdown]
# ## 5. One allocation, sized for what the tasks claim
#
# This is the decision the manager makes, and here it makes itself: two tasks that claim no
# GPU fit as two `--overlap` **steps** on one allocation. Tillicum permits **one**
# interactive allocation, so a second job for the second task is not a tidier version of
# this — it is asking for a thing the cluster will not give you twice, to run work that
# needs no device at all.
#
# Batch is for the other shape: a task that needs the node for hours, runs overnight, or
# must not wait behind anything. That is `poe agent-batch`, which submits a job that brings
# up its own GPU and **ends itself** when the agent exits. Neither of these tasks is that,
# so neither uses it.
#
# `job-up` is idempotent: run it twice and you get the same job back, which is correct, not
# a failure. It is held in `tmux` on the login node, so you can attach and watch — the
# `attach:` line it prints is the command.

# %%
sh("uv run poe job-up dev --gpus 1 --time 01:00:00")

# %% [markdown]
# ## 6. Both tasks, onto that one allocation
#
# One command each. `--exp-id` gives each run its own log directory, so two runs never write
# into one notebook. A launch refuses before anything expensive — a dirty workdir, an
# unfilled declared key, or no spare GPU for an agent that claims one — and every refusal is
# a GPU-hour not spent.
#
# Neither is refused here, because both declare `gpus: 0`.

# %%
run_a = sh("uv run poe agent-run TRIAL-A --job dev --agent smoke --exp-id trial-a")

# %%
run_b = sh("uv run poe agent-run TRIAL-B --job dev --agent smoke-2 --exp-id trial-b")

# %% [markdown]
# ## 7. Watch both, from here
#
# Three different questions, three commands:
#
# * `agent-status` — what each agent says it is doing, and what it has cost
# * `agent-logs --cells` — a notebook's cells, summarised **on the login node**, so a 4 MB
#   notebook costs a few hundred tokens and is never copied to the laptop
# * `agent-watch --once` — one supervision pass: poll, decide, act, log
#
# Two independent progress signals sit behind these: what an agent *says* (the status block
# its hooks keep) and what is *observed* (its notebook's mtime). A run that stops saying
# anything **and** stops writing is stuck; one that only stops saying things is not.
#
# Both should reach `round 2/2` within a couple of minutes. Poll, rather than watching
# continuously — each poll is an ssh round trip.

# %%
def _session(p):
    """The session id out of a launch's output."""
    return p.stdout.split("session ")[-1].strip().split()[0] if "session " in p.stdout else ""


session_a, session_b = _session(run_a), _session(run_b)
print("task A:", session_a or "(read it from the launch output above)")
print("task B:", session_b or "(read it from the launch output above)")

# %%
for _ in range(10):
    p = sh("uv run poe agent-status", quiet=True)
    print(p.stdout.strip() or "no remote agents")
    if p.stdout.count("round 2/2") >= 2:
        print("\nboth tasks reported done")
        break
    time.sleep(30)

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
# ## 10. What the two agents produced
#
# One notebook each, under their own run roots, carrying the `nvidia-smi` output of the GPU
# that actually answered. That is the artifact this whole exercise exists to produce, and
# it is read in place on the login node — `juplit cells` summarises it there, so a large
# notebook costs a few hundred tokens and never crosses to the laptop.
#
# Nothing was pushed and there is nothing to review. That is the point: `hc --full` already
# proved push on both machines with a `--dry-run`, so a trial run has nothing left to prove
# about git, and a pull request you would only close is friction with no payoff.

# %%
for label, session in (("task A", session_a), ("task B", session_b)):
    print(f"\n===== {label} =====")
    if session:
        sh(f"uv run poe agent-logs {session} --cells")
    else:
        print("(read the session id from the launch output above)")

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
sh("uv run poe job-down dev")

# %%
sh("uv run poe flush --older-than 0d --dry-run")

# %% [markdown]
# Re-run that without `--dry-run` once you have read what it would remove.

# %% [markdown]
# ## What this proved, and what it did not
#
# | Proved | The row, or the step |
# |---|---|
# | The local footprint is real | `.envrc` at 0600 and `ssh config`, each carrying a `·` note saying what `init` did |
# | ssh reaches the login node | `reachable` |
# | Every key a live channel needs is filled | `my keys`, which demands only what `config/notify.yaml` turns on |
# | You can be reached | `notify send` under **both** headings — `--send` really delivered from each |
# | Git can reach the repo from both machines | the `github …` rows, one per heading |
# | It can **push**, not just read | `github … push`, from `hc --full`'s `--dry-run` probe |
# | Claude works headlessly on **both** machines, and costs something | `agent credential`, under each heading — `hc --full` asserts `total_cost_usd > 0` |
# | An allocation outlives the ssh that asked for it | `allocation probe`, in the mode you configured |
# | An allocation comes up and is attachable | `job-up`, and the `attach:` line it printed |
# | Staging refuses to launch onto a dirty tree | `agent-run` preflighted before spending |
# | Two tasks share one allocation as steps | both launched onto `dev`, neither refused |
# | An agent runs, reports rounds, and produces something | each wrote a notebook under its run root |
# | Progress and spend are visible from here | `agent-status`, `agent-logs --cells`, `status` |
# | The supervisor decides and records | `agent-watch --once` |
#
# It did **not** prove that a kill or a lease renewal works: these tasks finish in two
# minutes, long before any supervision threshold fires. It did not exercise **batch** — for
# tasks this small that would be the wrong call, and making the right call is the thing
# being demonstrated. And it proved nothing about a cluster-side `.envrc`, because no
# shipped agent declares a key; the first agent of yours that does is the first time those
# rows say anything.
#
# ## When something fails
#
# Every row says which machine it is about, so the first question — *where do I fix this?*
# — is answered by the heading it sits under.
#
# | Symptom | Where to look |
# |---|---|
# | A cell hangs, then times out | The authenticated ssh session died. Re-open it; re-run. |
# | `reachable` MISSING, `not authenticated` | Open a terminal, `ssh tillicum-login`, answer 2FA, leave it open. |
# | Everything behind it `SKIPPED` | That is the point: one broken link, not eight problems. A skip is never a pass. |
# | `my keys` MISSING | Fill them in *this laptop's* `.envrc`. It only ever asks for the channels you turned on. |
# | `notify send` MISSING under the login node | The cluster could not send — a different egress path from your laptop's. |
# | `agent credential` MISSING | `ssh tillicum-login` and run `claude` once, interactively. |
# | `github …` MISSING | That machine has no git credential for the repo. `gh auth login` there, or a PAT in git's credential store. |
# | `github … push` MISSING | It can read but not write. The credential needs the repo scope. |
# | `clone` says `not cloned yet` | Not a fault. A workdir is created by the first launch, not by setup. |
# | `clone` names a different repo | That workdir was staged from an older `repo:`. The `fix:` says how. |
# | `worktree` MISSING | Something is uncommitted in the staged repo, and a launch will refuse it. |
# | Agent stuck at `needs_human` | `poe agent-status` names what it is waiting on. |
# | Batch job never leaves `PENDING` | `poe status`. The queue is the queue; that is not a failure. |
