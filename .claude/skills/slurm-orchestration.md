---
name: slurm-orchestration
description: Drive Tillicum from a local Claude Code session — get an allocation, stage a repo, launch and supervise remote agents, follow a run, hand off, tear down. Use whenever working in the slurm-agent repo or asked to run something on Tillicum.
---

# Orchestrating Tillicum

You are on the researcher's laptop. It is the only machine that can reach Tillicum — UW 2FA
on a network no sandbox is on — so everything here runs locally and reaches the cluster
over ssh.

## Before anything

`poe hc`. It takes a second and rules out the usual cause of "everything is broken": a
dropped `ControlMaster` after a network change. If it fails on `cluster identity`, run
`ssh tillicum-login` once to re-auth, then try again.

On a fresh clone, `poe init` instead — it creates the footprint and finishes by really
sending a test notification, so setup ends in a proof.

## Getting compute

```bash
poe job-up remote_dev --gpus 2 --time 04:00:00
```

**Tillicum permits one interactive allocation.** If one is already up, this returns it —
that is correct, not a failure. Many agents share one allocation as job steps, so you
rarely need a second. If you need to run something while that allocation is busy, use
batch.

## Launching an agent

```bash
poe agent-run TASK-104 --job remote_dev --agent experiment-runner
poe agent-batch TASK-104 --agent experiment-runner --time 12:00:00   # overnight
```

A launch refuses rather than half-working: a dirty workdir, a missing env key, or an
allocation with no spare GPU all stop it before anything is submitted. Every refusal is a
GPU-hour not spent — read the message, fix the named thing, re-run.

Prefer **batch** whenever a human does not need a shell on the node. A batch job ends when
the agent's process exits, so nothing can be left holding GPUs.

## Following a run

```bash
poe status                    # running / queued / completed / failed
poe agent-status              # just the live ones, with what they are waiting on
poe agent-logs <session> --cells
poe agent-watch               # the supervision loop
```

`agent-watch` kills on named thresholds and **proposes** renewals rather than taking them —
renewing means reading the notebook first, which is your judgement, not a rule's. Nothing
is lost if you stop the loop: it holds no state and rebuilds everything from the next poll.

## Handing off

The session notebook is the record, not this conversation. `poe session-new <name>`
scaffolds one; commit it with its outputs.

## Tearing down

```bash
poe agent-kill <session> --reason "wrong config"   # one agent, not its neighbours
poe job-down remote_dev                            # the whole allocation
poe flush                                          # tidy finished runs out of status
```

`flush` keeps failures by default. Their `agent.err` is the only record of why they died,
so drop them with `--failed` only once you have read them.

## Two things never to do

- **Never let an agent cancel a shared interactive allocation.** It would take down its
  neighbours. Kill the step, or use batch, where the job is the agent's alone.
- **Never write orchestrator files inside a staged repo.** A dirty tree blocks the next
  launch, and that block is what stops an experiment measuring unreviewed code. Everything
  goes in the run root, via `$SLURM_AGENT_RUN_DIR`.
