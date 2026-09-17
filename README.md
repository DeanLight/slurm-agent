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
`hc --full`, and it stops. Commit it with its outputs — they are the proof this
clone works.

After that there is one command left — you talk to the manager, and a task id is the whole
interface:

```
cd ~/src/slurm-agent && poe manage
> Pick up TASK-118 on Tillicum. Keep me posted on progress and spend.
```

Spinning work up, asking how it is going and reading what it cost are all the same command,
because they are all the same conversation:

```bash
poe manage "Pick up TASK-118 on Tillicum"
poe manage "How are my runs going, and what have they cost?"
```

It reads the row in Notion, works out what the task asks for and which repo it is in, sizes
the compute, writes the agent config, launches onto Tillicum, supervises, and reports back.
You never name an allocation or a workdir.

From a script or notebook it is the same manager and the same conversation, printing its
reply and exiting — so one session can feed the next:

```bash
IDS=$(claude -p 'Open two Notion tasks: <A>, and <B>. Reply with the ids, one per line.')
echo "$IDS"
poe manage -p "Pick up $IDS on Tillicum. Tell me what each produced and what it cost."
```

`poe manage` is not a wrapper in the sense that matters: `claude` started in this root **is**
the manager, because `CLAUDE.md`, `.claude/skills/slurm-orchestration/SKILL.md`, `.mcp.json`
and `.claude/settings.json` say so and Claude Code reads all four by itself. Delete the task
and that still holds. What it adds is only what a command line can carry and a checked-in
file cannot: the Remote Control name, `--continue` so you reach the session that launched
your runs rather than a fresh one, and the opening message. Logs go to stderr and
`--output-format` defaults to `text` under `-p`, so stdout is the reply and `$( )` is all
you need.

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
| `poe init` | Create the local footprint, then prove it works |
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
| `poe spend` | What the scheduled polls recorded about cost |
| `poe monitor-*` | The change-gated usage digest and its schedule |
| `poe session-new NAME --into DIR` | Scaffold a session notebook in the repo it is about |

`poe --help` is the full inventory.

`poe status` names every run — running, queued, completed, failed — with what each one cost
in GPU time. It reads `squeue`, `sacct` and the run roots each time you ask, so there is
nothing local to go stale, and `poe flush` prunes run roots rather than a list. What the
agents spent in tokens is a separate bill, reported by `poe monitor-*`; the two figures are
never added together.

## How a run is supervised

`config/supervision.yaml` says what "stuck" means, so a kill is a rule firing rather than a
judgement call — and every kill records the threshold that fired. Two progress signals stay
independent: what the agent *says* about itself, and what the filesystem *observed*. A
detector for a stuck agent must not depend on the stuck agent's own account.

**Rules kill; you renew.** Renewing means reading the notebook first, which no threshold
can do.

## How it reaches you

It does not. There is no email, no Slack webhook, no sender to configure — the manager is
a Claude Code session you are already talking to, so it tells you. Start it with `claude
--remote-control` and that conversation is also at claude.ai/code and in the Claude app,
while Claude keeps running on this laptop, the only machine that reaches Tillicum.

The scheduled usage poll appends to `usage.jsonl` under the run root **on the cluster**;
`poe spend` reads it back, and the manager reads that. A cron entry has nobody to talk to;
the manager does.

## What it never does

Reach Tillicum from anywhere but this laptop. Push anything from the compute node back to
you. Write secrets for you. Write its own files inside a repo it staged. Let the scheduled
monitor spend money.
