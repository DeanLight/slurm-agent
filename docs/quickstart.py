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
# Run this the first time you set this repo up on a laptop, and again whenever you doubt
# it. It does one job: get the environment and config right, and prove it.
#
# 1. `poe init` — create the local footprint, and get one report of everything
# 2. fill in `.envrc`
# 3. `poe hc --full --send` — prove every wire carries current, on **both** machines:
#    Claude authenticated, git authenticated and able to push, an allocation that outlives
#    the ssh that asked for it, and a message that really arrives
# 4. hand the actual work to the manager
#
# **Step 4 is the whole point, and it is one sentence typed at Claude.** You do not bring
# up allocations, launch agents or poll them yourself — the manager does that, decides how
# much compute the work needs, and reports progress and spend back to you. Driving it by
# hand here would just be a slower, more brittle copy of what it already does, so this
# notebook does not do it.
#
# So there is nothing to push, no branch, and no pull request to review. Step 3 proves push
# from both machines with a `--dry-run`; after that, what agents produce is read in place on
# the cluster and summarised to you.
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
# A few cents. `hc --full` makes one real headless Claude call on each machine and holds a
# one-minute allocation to prove it survives the ssh closing. Nothing here runs an agent.
#
# What the *work* costs is the manager's to report, and it keeps two figures apart on
# purpose: **GPU-hours**, which are real money on a real account, and **agent tokens**,
# which the CLI prices at API list rates even under a subscription. Never add them together,
# and never reconcile either against an invoice.

# %% [markdown]
# ## 0. The harness
#
# One helper, because every step below is "run a `poe` task and read what it says". It
# never raises: a failing check is something to *read*, and `poe init` failing the first
# time is the expected path, not an accident.

# %%
import os
import subprocess
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
# no registry behind it. The report below heads one group per line of this, so you do not
# have to hold the mapping in your head while reading it.
from slurm_agent.config import load_agents  # noqa: E402

for kind, cfg in load_agents().items():
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
# ## 4. Hand the work to the manager
#
# That is the setup done, and it is where this notebook stops. **You do not drive
# allocations and launches by hand.** That is the manager's job, and doing it yourself here
# would only be a slower, more brittle copy of what it already does.
#
# Open Claude Code in this repo and tell it the work:
#
# ```
# claude
# > Run two small tasks on Tillicum for me: <task A>, and <task B>.
# > Keep me posted on progress and spend.
# ```
#
# `.claude/skills/slurm-orchestration/SKILL.md` auto-loads from this repo — you do not have
# to name it — and it is what makes that sentence enough. It tells the manager to:
#
# 1. run `poe hc --full` first, so nothing is spent on work that cannot finish;
# 2. **decide the compute itself** — Tillicum permits one interactive allocation, so tasks
#    that claim no GPU (`gpus: 0`) run as steps on one shared allocation, and only work that
#    needs a node for hours or runs unattended goes to `poe agent-batch`;
# 3. **write an `agents/<kind>.yaml` for each task you named**, if one does not exist —
#    which repo and branch it works in, where it stages, where its output goes, what it may
#    run and spend — and tell you what it wrote before launching it. That file is the audit
#    surface: it is how you see what an agent was allowed to do;
# 4. bring up exactly one allocation, sized for whatever actually claims a device, and
#    launch each task onto it;
# 5. poll, and **report back in its own words** — which task, how far along, what it has
#    cost, and whether anything needs you;
# 6. drop the allocation when the last task is done, because it is the only thing that costs
#    money while nobody is looking.
#
# Ask it for an update whenever you want one; it re-derives everything from `squeue`,
# `sacct` and the run roots on the cluster, so there is no stale local state to go wrong and
# closing your laptop mid-run loses nothing.
#
# If you want the raw view yourself, the same commands are there — `poe status`,
# `poe agent-status`, `poe agent-logs <session> --cells`, `poe job-down <name>`. But the
# point of this repo is that you should not need them.

# %% [markdown]
# ## What this proved
#
# Every line of it is a row in the report above, so you never have to take this table's
# word for it.
#
# | Proved | The row |
# |---|---|
# | The local footprint is real | `.envrc` at 0600 and `ssh config`, each with a `·` note saying what `init` did |
# | ssh reaches the login node | `reachable` |
# | Every key a live channel needs is filled | `my keys`, which demands only what `config/notify.yaml` turns on |
# | You can be reached | `notify send` under **both** headings — `--send` really delivered from each |
# | Git can reach the repo from both machines | the `github …` rows, one per heading |
# | It can **push**, not just read | `github … push`, from `hc --full`'s `--dry-run` probe |
# | Claude works headlessly on **both** machines, and costs something | `agent credential`, under each heading — `hc --full` asserts `total_cost_usd > 0` |
# | An allocation outlives the ssh that asked for it | `allocation probe`, in the mode you configured |
# | This clone knows which repos it manages | one `staged repo ·` heading per `agents/<kind>.yaml` |
#
# That is everything an agent needs in order to start and to finish. What it does **not**
# prove is anything about an agent itself — whether a supervision threshold fires, whether a
# lease renews, whether a task's own code works. Those are answered by running real work,
# which is the manager's job and not this notebook's.
#
# A cluster-side `.envrc` is also unproven, because no shipped agent declares a key. The
# first agent of yours that does is the first time those rows say anything.
#
# ## When something fails
#
# Every row says which machine it is about, so the first question — *where do I fix this?*
# — is answered by the heading it sits under.
#
# | Symptom | Where to look |
# |---|---|
# | A cell hangs, then times out | The authenticated ssh session died. Re-open it; re-run. |
# | `hc` passes but a launch fails later | Tell the manager what it said. `hc` proves the machines, not an agent's own code. |
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
