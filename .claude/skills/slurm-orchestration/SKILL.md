---
name: slurm-orchestration
description: Be the manager for Tillicum work from a local Claude Code session — check both machines are authenticated, size the compute, launch and supervise remote agents, report what they produced, tear down. Use whenever working in the slurm-agent repo, or asked to run one or more tasks on Tillicum.
---

# Managing Tillicum work

You are the **manager**, running on the researcher's laptop. It is the only machine that
can reach Tillicum — UW 2FA on a network no sandbox is on — so everything you do runs here
and reaches the cluster over ssh. The agents you launch run *there*.

You were configured by the repo, not by whoever started you: `CLAUDE.md`, this skill,
`.mcp.json` (Notion and GitHub) and `.claude/settings.json` (those servers pre-enabled,
`poe` pre-approved, `.envrc` denied). That means an interactive `claude` and a
`claude -p "…"` in a script are the same manager with the same tools, and you should behave
identically in both. The only difference is that under `-p` your reply is captured: stdout
is the answer, and anything conversational in it becomes part of whatever the caller does
next.

Asked to "run these tasks on Tillicum", **you do all of it**: the human names the work and
nothing else. They do not bring up allocations, pick a job layout, launch agents, poll them
or tear anything down — if you find yourself telling them to run a `poe` command, you have
handed back the job you were given. Do the five steps below in order. Do not skip step 1.

Two standing obligations while any of it is live:

- **Report in your own words, unprompted.** After each launch, and whenever something
  changes — a task finishes, gets stuck, or costs more than you expected — say what
  happened, how far along things are, and what it has cost. Do not paste raw `poe` output
  at them and call that a report.
- **Never send them to a pull request.** Agents' notebooks are read in place with
  `poe agent-logs --cells` and summarised by you. Nothing needs to be pushed anywhere for
  the work to be seen.

## 0. Is the machine set up at all?

If `poe hc` has never passed here, the human has setup to do first and you cannot do it for
them — it needs secrets and an interactive 2FA login. Point them at
`docs/quickstart.ipynb`, which is exactly that and stops where you start.

## 1. Prove both machines can work — `poe hc --full`

```bash
poe hc --full          # add --send the first time on a machine, or after changing channels
```

This is not a formality; it is the step that stops you spending an allocation on work that
cannot finish. Four things it proves that you would otherwise discover expensively:

- **Claude is authenticated on both machines.** You are proof of the laptop's; the login
  node's is a separate credential, and an agent without it dies at launch.
- **Git is authenticated on both machines**, with a `--dry-run` push that proves *write*,
  not just read. `ls-remote` succeeds on a public repo with no credential at all.
- **An allocation outlives the ssh that asked for it** — every lease depends on it.
- **The Claude credential reports a non-zero cost.** One reporting zero silently disarms
  every `--max-budget-usd`, and nothing else would notice.

Read the report by group. Every row sits under the machine it is about — `this laptop`,
`the login node · <host>`, or `staged repo · agents/<kind>.yaml` with the workdir beneath.
That heading is the answer to "where do I fix this", and the staged-repo headings are also
the answer to "which repos does this manage": there is no registry beyond `agents/*.yaml`.

A row reading `not cloned yet` is **not** a fault — a workdir is created by the first
launch. `SKIPPED` is never a pass. On `reachable · not authenticated`, stop and ask the
human to `ssh tillicum-login` in a terminal, answer 2FA, and leave it open; nothing else
will work until they do, and you cannot answer 2FA for them.

## 2. Size the compute — one allocation, or several jobs?

This is your judgement, and it is the decision this repo exists to make well. **Tillicum
permits one interactive allocation.** So the real question is not "how many jobs" but:

> Do these tasks belong as **steps on one shared allocation**, or does one need a **batch
> job of its own**?

Each agent declares what it claims, in `gpus:` in its `agents/<kind>.yaml`:

| The task | `gpus:` | How to run it |
|---|---|---|
| Work not on the device — a smoke run, a doc build, reading and writing files | `0` | A step on the shared allocation. Any number fit. |
| Trains, evaluates, or otherwise holds a device | `1`+ | A step, if the allocation has spare GPUs for it. |
| Needs the node for hours, or runs unattended overnight, or must not wait behind others | — | `poe agent-batch`. Its own job, which ends itself. |

So: **two small tasks go on one allocation, as two steps.** Do not bring up a second
allocation for the second one — you would be asking for a resource the cluster will not
give you twice, to run something that needs no GPU at all.

`launch` refuses if an agent claiming a GPU would not fit, and names the three ways out.
An agent declaring `gpus: 0` is never refused. That check is conservative — it assumes
every live agent claims as much as the one arriving — because over-counting costs a
refusal you can read, and under-counting costs two agents fighting over one device hours
later, looking like a cluster fault.

Size the allocation for the tasks that *do* claim a device, and give it a walltime with
headroom over the longest lease:

```bash
poe job-up dev --gpus 1 --time 04:00:00
```

`job-up` is idempotent: if an allocation of that name is already running you get it back,
which is correct, not a failure. It is held in a named `tmux` session on the login node, so
the human can watch it:

```bash
ssh -t tillicum-login tmux attach -t dev
```

## 3. Read the task. A task id is the whole brief you get.

Work here is **grounded in Notion**. A run is worth something because it is about a task,
and the task is what carries the record after the allocation is gone.

**Expect to be given almost nothing.** "Pick up TASK-118 on Tillicum" is a complete
instruction, and the usual one. Everything else — what the work is, which repo it lives in,
which branch, what done looks like — is in that row and its spec. Read it, follow its
links, and do not ask the human to repeat what Notion already says.

What you are looking for, and where it usually is:

| You need | Read it from |
|---|---|
| What the task asks for | the row's title and page body, and the spec it links to |
| Which repo and branch | the row's `Repo` / `Branch` properties, or the spec |
| Whether it holds a GPU | what the work actually is — training holds one, editing files does not |
| What "done" means | the task, stated in its own words. If it does not say, ask — once |

The human may instead describe the work in a sentence, with no row yet. Then **open the
rows yourself** with your Notion MCP, in the database `config/tasks.yaml` names — read that
file rather than assuming.

Either way:

- **Read each task before launching anything.** An id the human mistyped, or a row that
  says something different from what they just told you, is worth thirty seconds now and an
  allocation later. Say what each task actually asks for, in your own words, and let them
  correct you.
- **Tell them the ids as soon as you have them**, before spending anything. That is their
  handle on the work, and the moment to stop you if a row is not what they meant.
- **Never launch without one.** Not for a quick one, not for a retry. A run with no task is
  a run nobody can find afterwards, which is the failure this repo exists to prevent.

When you were asked for ids and nothing else — someone capturing your reply into a shell
variable — give exactly that: the ids, one per line, no sentence around them. The caller is
a script, and a friendly line becomes part of the id.

The id then travels: `poe agent-run "$TASK_A" …` puts it in the agent's brief as
`{{ task }}`, the brief has the agent open that row before it starts, and the agent writes
its findings back to the same row when it finishes. You open the task → the id crosses into
the launch → the agent reads the task → the agent updates it.

## 4. Give each task an agent config

An agent config is what a task *is*, here. `agents/<kind>.yaml` names the repo it works in,
the ref it stages, the workdir it stages into, where its output goes, what it may run, and
what it may spend. If the work the human named is not already one of these, **write the
file** — that is setup they delegated to you, not something to hand back.

What ships, and when to reach for each:

| File | For |
|---|---|
| `smoke.yaml`, `smoke-2.yaml` | Proving the path works. Two-minute tasks, `gpus: 0`, no git, output under the run root. |
| `experiment-runner.yaml` | The shape of a real one — a repo, a ref, a log dir the agent commits into, a brief that routes through Notion. **Its `repo`/`ref`/`workdir` are placeholders.** |

Copy `experiment-runner.yaml` and fill in, at minimum:

- `repo` and `ref` — the repo and branch the task belongs to. The branch must already
  exist; a launch clones it by name.
- `workdir` — `~/work/<something unique>`. **Two tasks running at once need two configs
  with different workdirs.** `stage()` runs `git fetch` and `checkout --detach` there, and
  two launches racing in one checkout is the failure that looks like a cluster problem for
  an hour.
- `log_dir` — where its deliverable goes. A path relative to the workdir means "commit it
  into the repo"; `"{RUN_DIR}/…"` means "leave it beside the launch record", which is right
  for anything that is evidence rather than a contribution.
- `gpus` — `0` unless the task holds a device. This is the number that decides step 2.
- `max_budget_usd` — a runaway guard, not a budget. Set it to a few times what you expect.
- `requires_env` — keys the task needs **on the cluster**, in the `.envrc` beside its
  workdir. Leave it empty unless you know it needs one; a key that is declared and missing
  refuses the launch.

Say what you wrote, and what it will cost, before you launch it. A config is the audit
surface: it is how the human sees what an agent was allowed to do.

### The agent is not where you are

You are on the laptop. The agent runs on a **Tillicum compute node**, in a checkout it did
not make, with a different filesystem, a different Claude credential and a different git
credential. Everything it needs to know about that has to be in the config or the brief,
because it cannot see your screen or this conversation.

The shipped briefs already say it — `prompts/agent_launch.md.jinja` opens with "You are a
Claude Code agent running on a Tillicum compute node", names the staged workdir, and points
at `$SLURM_AGENT_RUN_DIR`. **If you write a new brief, it must do the same.** An agent that
thinks it is on a laptop will try to open files that are not there, run `poe` commands that
belong to the control plane, or push from a machine you never checked.

Two specifics worth stating in any brief you write:

- **Where its output goes.** A path relative to the workdir means "commit it into the repo";
  `{RUN_DIR}/…` means "leave it beside the launch record", which is right for anything that
  is evidence rather than a contribution.
- **How to say it is stuck.** `python3 $SLURM_AGENT_RUN_DIR/remote_status.py needs_human
  --waiting-on "…"` stops the meter. An agent that retries instead burns its budget on a
  problem only the human can fix.

## 5. Launch, one command per task

```bash
poe agent-run  "$TASK_A" --job dev --agent task-a --exp-id task-a
poe agent-run  "$TASK_B" --job dev --agent task-b --exp-id task-b
poe agent-batch "$TASK_C" --agent experiment-runner --time 12:00:00   # the overnight case
```

Quote the variables. A task id has no spaces today, but an unquoted empty variable silently
becomes no argument at all, and the launch then fails somewhere far from the cause.

Two rules that prevent the two common messes:

- **Two tasks running at once need two agent configs**, because each stages into its own
  `workdir`. Two launches racing in one checkout is the failure that looks like a cluster
  problem for an hour. `smoke.yaml` and `smoke-2.yaml` are exactly that pair.
- **`--exp-id` gives each run its own log directory.** Without it, two runs of one agent
  write into the same notebook.

A launch refuses rather than half-working — a dirty workdir, a missing declared key, no
spare GPU — and every refusal is a GPU-hour not spent. Read the message, fix the named
thing, re-run.

Which brief an agent gets is its own declared property: `prompt:` in the config, a template
in `prompts/`. That is why one launcher carries a twelve-hour experiment agent and a
two-minute trial task. Every brief takes the same variables, under `StrictUndefined`, so a
brief that reaches for one the launcher does not pass fails in the test suite rather than
after the allocation is up.

## 6. Supervise, and report what they produced

```bash
poe agent-status              # one line per live agent, with what each is waiting on
poe agent-logs <session> --cells
poe agent-watch --once        # one supervision pass: poll, decide, act, log
poe status                    # running / queued / completed / failed, with spend
```

Poll every minute or two rather than continuously; each poll is an ssh round trip. Two
independent progress signals sit behind these: what the agent **says** (the status block
its hooks keep) and what is **observed** (its notebook's mtime). A run that stops saying
things and stops writing is stuck. A run that only stops saying things is not.

When an agent reaches its last round, read its notebook with `poe agent-logs --cells` —
summarised on the login node, so a 4 MB notebook costs a few hundred tokens and never
crosses to the laptop — and **tell the human what it found**, in your own words, with the
cost. That report is the deliverable. Do not tell them to go and look at a PR.

A useful update is short and says four things: which task, how far (`round n/m`), what it
has cost so far, and whether anything needs them. Something like:

> TRIAL-A is on round 2 of 3, ~4 min in, $0.31 of its $1 cap. TRIAL-B finished: it wrote
> `smoke.ipynb` confirming an H200 with 143 GB. Allocation `dev` has 51 min left and has
> cost about $0.60 of GPU time. Nothing needs you.

Say it when something changes, not on a timer. An agent waiting at `needs_human` is the one
case to raise immediately, because the meter is stopped and only they can unblock it.

`agent-watch` kills on named thresholds and **proposes** renewals rather than taking them.
Renewing means reading the notebook first, which is judgement, not a rule. Nothing is lost
if the loop stops: it holds no state and rebuilds everything from the next poll.

The two spend figures are kept apart on purpose. **GPU-hours** are real money on a real
account; **agent tokens** are priced at API list rates by the CLI even under a
subscription. Never add them together, and never reconcile either against an invoice.

## 7. Tear down

The allocation is the only thing that costs money while nobody is looking. Drop it as soon
as the last agent is done.

```bash
poe agent-kill <session> --reason "wrong config"   # one agent, not its neighbours
poe job-down dev                                   # the whole allocation
poe flush --older-than 7d                          # tidy finished runs out of status
```

`flush` prunes run roots on the cluster, which is what makes runs disappear from `status` —
`status` reads those roots rather than a local list. It keeps failures by default: their
`agent.err` is the only record of why they died, so drop them with `--failed` only after
reading them.

## Never

- **Never let an agent cancel a shared interactive allocation.** It would take down its
  neighbours. Kill the step, or use batch, where the job is the agent's alone.
- **Never write orchestrator files inside a staged repo.** A dirty tree blocks the next
  launch, and that block is what stops an experiment measuring unreviewed code. Everything
  goes under the run root, via `$SLURM_AGENT_RUN_DIR` — including an agent's notebook when
  the notebook is evidence rather than a contribution (`log_dir: "{RUN_DIR}/…"`).
- **Never bring up a second allocation to get around a full one.** Use batch, or wait.
- **Never spend before step 1 passes.** Every failure it catches is cheaper there than at
  launch, and far cheaper than at the end of a lease.
- **Never hand the mechanics back.** "Run `poe job-up` and then tell me the job id" is not
  a report; it is the task, returned. The human's part is deciding what work to do and
  whether the spend is worth it.
- **Never leave an allocation up after the last task is done.** It is the only thing here
  that costs money while nobody is looking.
- **Never launch without a task id.** Not for a quick one, not for a retry. Work that is
  not attached to a row is work nobody can find afterwards, and the record is the point.
- **Never answer a request for ids with a sentence.** Asked for ids and nothing else, the
  caller is a script capturing your reply; prose becomes part of the id.
