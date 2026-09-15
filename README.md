# slurm-agent

Drive Tillicum from a local Claude Code session: get an allocation, stage a repo, launch
Claude agents **on the compute node**, supervise them against named thresholds, and hear
about it only when a human is actually needed.

Tillicum sits behind UW 2FA on a network only the researcher's laptop is on, so no sandbox,
cloud session or CI runner can reach it. This repo runs on that laptop, on purpose.

## Setup

See [docs/setup.md](docs/setup.md). Short version:

```bash
uv sync --all-groups && poe hooks && poe init
```

Then run **[docs/quickstart.ipynb](docs/quickstart.py)** once on the new machine
(`poe nb` pairs it first). It does setup and nothing else: `init`, fill `.envrc`,
`hc --full --send`, and it stops. Commit it with its outputs — they are the proof this
clone works.

After that you never touch a `poe` command again — you talk to the manager. Interactively:

```
> Run two small tasks on Tillicum for me: <task A>, and <task B>.
> Keep me posted on progress and spend.
```

or from a script or notebook, where its reply feeds the next thing you say:

```bash
IDS=$(poe ask "Open two Notion tasks: <A>, and <B>. Reply with the ids, one per line.")
poe ask "Run $IDS on Tillicum. Tell me what each produced and what it cost."
```

`.claude/skills/slurm-orchestration/SKILL.md` auto-loads and makes that sentence enough. It
opens a Notion task for each piece of work (`poe task-new`, whose id it passes straight
into the launch), sizes the compute itself — one interactive allocation with several tasks
as steps, or a batch job for work that needs a node to itself — launches, supervises,
reports what each agent found and what it cost, and drops the allocation when the last one
is done. Each agent updates its own Notion row, so the run is findable long after the
allocation is gone.

## The one rule

**The cluster is the only source of truth. The laptop holds nothing it cannot rebuild.**

Everything reported here is derived from the SLURM queue plus files on the cluster's shared
filesystem. So a closed laptop loses nothing, two sessions agree, and supervision is a poll
loop you can stop and restart at will.

## Commands

| Command | What it does |
|---|---|
| `poe init` | Create the local footprint, then prove it works by really sending |
| `poe healthcheck` / `poe hc` | Verify. Fast by default; `--full` adds the slow proofs |
| `poe job-up NAME` | Bring up an allocation, or reattach to the live one |
| `poe job-status` | Every allocation of mine |
| `poe job-shell NAME` | A shell on the compute node |
| `poe job-down NAME` | Cancel the allocation |
| `poe agent-run TASK --job J --agent K` | Stage a repo and launch an agent on an allocation |
| `poe agent-batch TASK --agent K` | Submit the same agent as a self-terminating batch job |
| `poe agent-status` | One line per live agent, with what it is waiting on |
| `poe agent-logs S --cells` | Read a remote notebook in place |
| `poe agent-watch` | The supervision loop: poll, decide, act, log |
| `poe agent-kill S --reason R` | Stop one agent, not its neighbours |
| `poe agent-continue S` | A fresh lease on the same notebook |
| `poe status` | Running, queued, completed, failed |
| `poe flush` | Drop finished runs from `status` |
| `poe notify-test` | Really send, from here and from the cluster |
| `poe monitor-*` | The change-gated usage digest and its schedule |
| `poe session-new NAME --into DIR` | Scaffold a session notebook in the repo it is about |
| `poe ask "..."` | Ask the manager to do something; its reply is stdout, for `$( )` |
| `poe agent-run TRIAL-A --job J --agent smoke` | A two-minute trial task — see the quick start |

`poe --help` is the full inventory.

## How a run is supervised

`config/supervision.yaml` says what "stuck" means, so a kill is a rule firing rather than a
judgement call — and every kill records the threshold that fired. Two progress signals stay
independent: what the agent *says* about itself, and what the filesystem *observed*. A
detector for a stuck agent must not depend on the stuck agent's own account.

**Rules kill; you renew.** Renewing means reading the notebook first, which no threshold
can do.

## What it never does

Reach Tillicum from anywhere but this laptop. Push anything from the compute node back to
you. Write secrets for you. Write its own files inside a repo it staged. Let the scheduled
monitor spend money.
