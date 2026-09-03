# Design — [slurm-agent] Local Claude Code orchestration of Tillicum jobs and usage monitoring

- **Task:** TASK-101 · **Branch:** `claude/slurm-agent-orchestration-v8jauv`
- **Spec:** [[slurm-agent] Local Claude Code orchestration of Tillicum jobs and usage monitoring](https://app.notion.com/p/3c70dbff564781bdadfee2ec6f1d65f0) (approved)
- **Repo:** `DeanLight/slurm-agent` — greenfield, generated from `DeanLight/juplit_template`

## The one idea this design rests on

**The cluster is the only source of truth. The laptop holds nothing it cannot rebuild.**

Every fact the local session reports — what is allocated, what is running, how far it got,
what it is blocked on, what it cost — is derived from two things that live on Tillicum: the
SLURM queue, and files on the shared filesystem. Nothing authoritative is written to the
laptop.

That single constraint answers most of the spec's hard requirements at once:

- **A closed lid loses nothing.** `agent-watch` is killed when the laptop sleeps and
  re-derives its whole world from one poll when it comes back. There is no local run
  registry to go stale, no reconciliation, no crash recovery path to write.
- **A second session sees the same thing.** Two Claude Code sessions polling the same
  cluster agree, because neither one owns the state.
- **Supervision is genuinely poll-only.** Nothing has to be pushed back to the laptop
  because the laptop is not where the truth is.

The design's job is to make that one read cheap: **one SSH round trip per poll cycle**,
regardless of how many agents are in flight (§1, `probe`).

## Changes to the spec's tentative interfaces

Per **Code Design**, the spec's sketched interfaces are intent. Four changed; everything
else is adopted verbatim, names and flags included.

- **`poe init` splits into `poe init` (create) and `poe healthcheck` / `poe hc` (verify).**
  - *Was:* the spec's `poe init` did both — "check/create local setup, report what's
    missing" — and juplit_template's `init` is `pre-commit install`.
  - *Now:* **`init` creates** the footprint (hooks, ssh entries, `.envrc` from the template,
    the cluster run root) and then runs a full healthcheck, so setting up ends in a proof.
    **`healthcheck` verifies** and creates nothing, with a fast tier by default and `--full`
    for the slow proofs. `poe hc` aliases it; `poe hooks` re-installs hooks alone.
  - *Why:* they run at completely different rhythms. Creating happens once per clone;
    verifying happens every time you move network or re-auth to Tillicum — and the single
    most common "everything is broken" cause is a dropped `ControlMaster`, which is a
    one-second check. A verify command worth typing that often cannot be the same command
    that provisions and sends email.
- **Budget splits into two numbers: GPU dollars and agent dollars.**
  - *Was:* one `$5.10` column in `agent-status`, and `max_budget_usd` in the agent config
    read as if it covered both.
  - *Now:* `gpu_usd` (observed: GPUs x elapsed x the rate in `cluster.yaml`) and `agent_usd`
    (the Claude run's own token spend, priced at API list rates). `max_budget_usd` maps to
    the CLI's `--max-budget-usd` and caps only the second. The kill thresholds are about
    the first.
  - *Why:* they are different money with different controls. Conflating them means the
    thresholds that protect \$0.90/GPU-hour get tuned against token spend.
  - *Auth stays the subscription, and the numbers still work.* Verified against the CLI
    while writing this design: `claude -p --output-format json` returns `total_cost_usd`
    together with a per-model breakdown carrying **`"costBasis": "list"`** — the CLI prices
    a run at API list rates and reports it the same way regardless of how the session
    authenticated. So "track the Claude spend as if it were API spend" is not something
    this repo has to build; it is what the CLI already reports, and `--max-budget-usd` is
    a ceiling on that same number. Nothing here needs an API key, and `claude_argv` never
    emits `--bare` precisely so the Tillicum subscription login is the credential.
  - *Read it as a runaway guard, not a bill.* Under a subscription the real limit is the
    plan's usage window, which the CLI does not expose as a number. `max_budget_usd` is
    therefore the answer to "this agent has gone into a loop", not to "how much did this
    month cost". Named that way in `agents/*.yaml` comments so nobody reconciles it
    against an invoice.
  - *Consequence:* `agent_usd` is **unknown mid-run** — the cost is reported at exit, not
    continuously — so `agent-status` shows `—` until the run ends. This costs us nothing,
    because `--max-budget-usd` enforces the cap inside the CLI whether or not we can see
    the running total.
- **`no_progress_for` is measured from the notebook, not the status block.**
  - *Was:* `supervision.yaml` had `status_stale_for` (status not updated) and
    `no_progress_for` (status advancing but `cells_done` unchanged) — both read out of the
    status block the agent writes about itself.
  - *Now:* `status_stale_for` still reads the status block; `no_progress_for` reads the
    **`.ipynb` mtime and size**, observed by the probe.
  - *Why:* a detector for "the agent is stuck" must not depend on the stuck agent's own
    account of itself. Two independent signals catch two different failures: an agent that
    dies without updating status (stale), and an agent that keeps talking while doing
    nothing (no progress). Same two keys, same two thresholds — only where the second one
    looks changed.
- **The status block is written by a Claude Code hook, and only *enriched* by the agent.**
  - *Was:* "The remote agent … keeps a small machine-readable status block beside it" —
    i.e. the agent remembers to write it.
  - *Was, in the first draft of this design:* a shipped `remote_status.py` shim the agent
    calls. **That was only half an answer and the review was right to call it out.** A shim
    fixes *how* a write happens; it does nothing about *whether* it happens, because the
    agent still has to remember to call it. Two different problems were being conflated.
  - *Now:* they are separated, and each gets the mechanism that actually solves it.
    - **Whether it happens — a ****`Stop`**** hook.** Claude Code fires `Stop` at every turn
      boundary. The hook runs `remote_status.py tick`, which rewrites `updated` and
      recomputes `cells_done` by counting executed cells in the `.ipynb`. Both are facts
      about the world, computable without asking the agent anything, so **liveness needs
      zero cooperation**: `status_stale_for` becomes "this session has not completed a turn
      in 30 minutes", which is true whether the agent is cooperative, confused, or wedged.
      A `SessionEnd` hook writes the terminal `finished` / `failed` state the same way.
    - **Whether it is safe when it happens — the shim.** Atomic write (temp file plus
      `os.replace`) and a fixed schema, so a mid-write kill leaves a readable block and the
      local reader never parses improvised JSON. This is the shim's *whole* justification
      now, and it is a real one — the spec's own Q&A demands the atomic write.
    - **What it means — the agent.** `round`, `waiting_on`, `needs_env` / `needs_human` are
      semantic and only the agent knows them, so it still calls
      `python $SLURM_AGENT_RUN_DIR/remote_status.py running --round 2/3`. The difference is
      that a forgotten call now costs *detail*, not *detection*.
  - *Delivery:* the hooks come from a `settings.json` written into the run root and passed
    as `claude --settings <path>` — never a file inside the staged repo (see the next
    deviation). This is also the third independent reason `claude_argv` must not emit
    `--bare`: `--bare` skips hooks entirely, which would silently disable liveness.
- **Nothing this repo writes ever lands inside a staged repo.**
  - *Was:* the status block "beside" the notebook, and (in this design's first draft) a
    `.slurm-agent/` directory referenced by a relative path inside the workdir.
  - *Now:* the run root on the cluster — `~/.slurm-agent/runs/<session_id>/` — holds
    `launch.json`, `status.json`, `settings.json`, `remote_status.py`, `agent.log`,
    `agent.err` and (in batch mode) the rendered `job.sbatch`. The launch prompt and the
    hooks address them by **absolute** path, exported as `SLURM_AGENT_RUN_DIR`.
  - *Why:* the review caught this, and it is worse than untidiness. `stage()` refuses to
    launch onto a dirty tree — that refusal is what stops an experiment silently measuring
    unreviewed code — so a repo that this tool itself dirties would make its own safety
    check fire on every second launch. Gitignoring would paper over it; keeping our files
    out of the tree removes it. The notebook is the one thing we *do* write inside the
    repo, and that is deliberate: it is the deliverable, and the agent commits it.
  - *Belt and braces:* `.slurm-agent/` goes in this repo's own `.gitignore` too, for anyone
    who later puts a run root inside a checkout.

## Two execution modes, because Tillicum allows one interactive job

The review surfaced a cluster constraint neither the spec nor the first draft accounted
for: **Tillicum permits one interactive allocation at a time.** That single fact reshapes
the design, and it also makes the batch mode the review asked for necessary rather than
optional.

### Interactive — one allocation, many agents

The cap is on *allocations*, not on agents. Agents attach to an allocation as
`srun --jobid=… --overlap` job steps, so **N agents share one allocation**, which is what
the first draft already did without noticing why it mattered.

What changes:

- `job-up` **refuses to create a second interactive allocation.** It reports the existing
  one and returns its handle, rather than submitting a request that will queue behind a job
  the same user is holding.
- `agent-run` **checks the allocation has spare GPUs** before adding a step. The allocation
  is sized once, for everyone on it; a fourth agent on a two-GPU allocation is a
  contention bug that would otherwise present as "the run is mysteriously slow".
- The `job-status` sketch in the spec, which showed two interactive allocations side by
  side, was not reachable. It now shows one interactive allocation plus any batch jobs.
- **An agent never cancels the allocation it is running on** — it is shared, and killing it
  would take down its neighbours. This is a correction to the review's "tell the agent to
  close its own job": correct for batch, actively wrong for interactive. Interactive
  teardown stays the manager's call, and `SessionEnd` only marks the state.

### Batch — for work that should not hold the interactive slot

`sbatch` jobs are not capped the same way, do not need a human present, and end by
themselves. That makes them the right home for exactly what the review described: overnight
work, hard work, and anything running in parallel with a human's interactive session.

- `poe agent-batch TASK --agent <kind> --time 12:00:00` stages, renders `job.sbatch` into
  the run root from a jinja template, and `sbatch`es it.
- **Self-termination is free here.** The batch script's last statement *is* the `claude -p`
  run, so the job ends when the agent's process exits. No hook, no reminder, no trust in
  the agent to tidy up — which is the strongest argument for preferring batch whenever a
  human does not need a shell on the node.
- **Everything downstream is unchanged.** Same staging, same `claude_argv`, same run root,
  same status block, same hooks, same `probe` (`squeue` already returns batch jobs), same
  `decide`. Batch is a *submission* difference, roughly 40 lines, not a second system.
- Leases do not apply. A batch job's walltime is a real deadline, so `decide` proposes
  nothing at the end of one; it escalates if the job hits `TIMEOUT` with the run unfinished.

`AgentConfig.mode: interactive | batch` picks the default per agent kind; the CLI command
chosen (`agent-run` vs `agent-batch`) overrides it.

### Polling the whole picture: `poe status` and `poe flush`

`agent-status` and `job-status` answer "what is live right now". Neither answers the
question a human actually asks between sessions — **what is running, what is queued, what
finished, and what failed** — because the last two are precisely the ones that have left
`squeue`. A finished job vanishes from the queue within minutes.

- **`poe status` is the poll.** Four sections, one screen: `running`, `queued`, `completed`,
  `failed`. Cheap enough to run in a loop, because it is still **one SSH round trip** — the
  probe script gains an `sacct` call alongside its `squeue` call and returns both.
- **History does not break the design's core rule.** "The cluster is the only source of
  truth" still holds, because the history is *also* on the cluster: `sacct` retains
  terminal job states, and the run root — `launch.json` plus the terminal `status.json` the
  `SessionEnd` hook wrote — persists after the job is gone. `status` reads those; the
  laptop still stores nothing it cannot rebuild, and two sessions still agree.
- **`poe flush` is how the list stays readable.** It prunes *run roots*, not a local
  dismissed-list — a local list would be exactly the unrebuildable laptop state this design
  refuses. Deleting the run root is what makes an entry leave `status`, on every machine at
  once.
- **Flush is deliberately timid, because a failed run's log is evidence.** Default is
  `--older-than 7d` and completed runs **only**: a success's evidence already lives in its
  committed notebook and its PR, so the run root is redundant. **Failures are kept unless
  you ask for them** with `--failed`, because `agent.err` is the only record of why an
  agent died and the spec requires that "why did my job die" always be answerable. A live
  session is never touched, and neither is anything inside a staged repo.

### Who tells the human, and when

The review asked for agents to announce their own completion so the manager only speaks
about failures. That is the split, and — following the ruling in Appendix C — agents now
reach the human **directly**, by email and Slack, from the compute node.

- **The agent announces itself, on two channels.** A PR comment for anything review-worthy
  (it already has the git credential and the GitHub MCP, and the spec already says a
  finished run points the human at its notebook *in its PR*), plus an actual email or Slack
  message so the human finds out without opening GitHub.
- **It is the ****`SessionEnd`**** hook that sends, not the agent's good intentions.** Same
  lesson as the status block: the hook writes the terminal state and then sends a message
  derived from that recorded state. An agent that forgets to say goodbye still says
  goodbye. Mid-run, the agent may also send on `needs_human` — the one case where waiting
  three days for the next digest is the wrong answer.
- **This does not cost the human more interrupts.** The spec's "interrupted twice, at most"
  holds unchanged, because the triggers are unchanged: something only a human can fix, or a
  run that finished and wants review. All that moved is *who* pushes the message. A run
  that is merely still going still generates nothing.
- **The local side speaks only about failures and silences:** crashed, `TIMEOUT`, killed by
  a threshold, or `finished` having never announced itself. That last one is the review's
  "tell me if they could not send the message", and it falls out of `decide` as one rule.

**The credential this needs, and the one mechanism it uses.** Sending from the compute node
requires an SMTP app-password or a Slack webhook on Tillicum. The spec forbade secrets on
the shared filesystem and the ruling reverses it — but there is **no new secrets mechanism**,
because the repo already has one:

- **`.envrc` is where secrets live** — gitignored, never committed, one per machine, sitting
  beside the code. That is already how the spec has an experiment repo's env work on
  Tillicum ("a missing one stops the agent at `needs_env` naming the exact variables, for
  the human to add to that directory's `.envrc`"), and the local manager now works the same
  way. One convention everywhere, rather than a bespoke credentials file for notifications.
- **The YAML declares key NAMES, never values.** `ManagerConfig.requires_env` names what the
  local session needs; `AgentConfig.requires_env` already names what a remote agent needs.
  Committed config stays reviewable and secret-free — "reviewing what an agent was allowed
  to do means reading a file" extends to "reviewing what it was given" — and the same
  `missing_env()` serves both sides.
- **Values reach the process through the environment, not through a reader we wrote.**
  `poe` loads `.envrc` for every task via `[tool.poe] envfile` (verified: it accepts both
  `export KEY=v` and bare `KEY=v`, and ignores comments — so no `direnv` dependency), and
  the remote launch already does `bash -lc 'source .envrc; exec …'`. `notify.py` and
  `remote_notify.py` read `os.environ`. **There is no credentials parser in this design.**
- `poe healthcheck` **asserts mode `0600`** on each `.envrc` holding declared secrets, and
  fails loudly otherwise. Tillicum is a shared filesystem; a group-readable app-password is
  the actual risk, and it is the kind of thing discovered late or never.
- Errors from `smtplib` / the webhook POST are scrubbed of the values of every declared
  `requires_env` key before they reach a log or a status block, so a misconfiguration
  cannot leak a secret into a committed notebook.

## 0. Justify existence

The default answer is *don't build it*. What follows is what survived.

### Cut outright

- **An SSH library** (`paramiko`, `fabric`, `asyncssh`). `subprocess` + the `ControlMaster`
  socket that `slurm-ops` already configures is what makes UW's 2FA a once-per-day event; a
  library that opens its own connections re-authenticates and buys nothing.
- **A local run registry / SQLite state file.** The cluster is the source of truth (above).
  A local registry is a second truth that can disagree with the first.
- **A push channel, callback endpoint or daemon on the node.** Out of scope in the spec, and
  the polling design does not need one.
- **Budget enforcement for agent spend.** `claude --max-budget-usd` already does it.
- **A retry/backoff framework.** An SSH failure during a poll is one skipped cycle; the next
  cycle re-derives everything. `except OSError: log and continue` is the whole handler.
- **A scheduler abstraction over SLURM.** Out of scope in the spec.
- **`asyncio` anywhere.** The Code Guide says async-first for I/O, and this is the one place
  it does not pay: every remote read in this repo goes through a single `ControlMaster`
  socket that serialises anyway, and the poll is *deliberately* one round trip (§1 `probe`).
  Concurrency would add a dependency and a failure mode to save nothing. **Stated as a
  deliberate deviation from the Code Guide, not an oversight.**
- **A cost model.** `gpus x elapsed_hours x rate`, one line, rate in `cluster.yaml`.
- **Any `Base*` class, ABC or Protocol.** There is one cluster, one agent runner, one
  notification channel per message. Email and Slack are two concrete functions and a
  `notify()` that fans out — nothing swaps a channel at runtime, so there is nothing for an
  interface to abstract.

### What earns its place

- **`remote.run` — one SSH primitive**
  - *Needs to exist:* yes. Every remote fact goes through it, and it is the single seam the
    entire test suite injects at.
  - *Already solved:* `subprocess.run(["ssh", host, cmd], capture_output=True)` is the body.
  - *Smallest form:* ~10 lines: build argv, run, raise `RemoteError` on non-zero with
    stderr attached. Callers take it as a parameter, so tests pass a dict-backed fake and
    no mocking library is needed.
- **`remote.probe` — the one-round-trip poll**
  - *Needs to exist:* yes — it is the design's load-bearing piece. A poll that costs one
    `squeue` plus one `cat` per agent per cycle is 1 + N round trips over a 2FA'd link
    every 5 minutes; a poll that costs one is a poll you can afford to run all day.
  - *Already solved:* no. But it is not new code so much as a **bundled shell script** piped
    to `ssh` on stdin — `squeue`, then a loop over `~/.slurm-agent/runs/*/launch.json`
    `cat`ting each status block and `stat`ing each notebook, emitting one JSON object.
    Nothing is installed on the cluster.
  - *Smallest form:* ~30 lines of `sh` in `slurm_agent/assets/probe.sh`, plus a ~15-line
    Python side that pipes it and parses the JSON.
- **Pydantic config models (`ClusterConfig`, `AgentConfig`, `SupervisionConfig`, `MonitorConfig`)**
  - *Needs to exist:* yes — "reviewing what an agent was allowed to do means reading a file"
    is a spec requirement, and a typo in `allowed_tools` must fail at load, not at launch.
  - *Already solved:* pydantic + `yaml.safe_load`. `extra="forbid"` is the whole validation
    story.
  - *Smallest form:* four `BaseModel`s and one 6-line `load_yaml(path, model)`.
- **`jobs` — allocation lifecycle**
  - *Needs to exist:* yes, this is the spec's first bullet.
  - *Already solved:* partly. `slurm-ops` has the `squeue` parsing and the SSH-node-config
    rewrite; both are ported (the repo stays public and unchanged — spec Q&A). What it does
    not have is *executing* rather than printing, and `salloc --no-shell`.
  - *Smallest form:* four functions over `remote.run`, ~90 lines total.
- **`staging` — repo/ref staging and env preflight**
  - *Needs to exist:* yes — "Tillicum holds many repos" and a launch onto a dirty or wrong
    tree is how an experiment silently measures the wrong code.
  - *Already solved:* `git` does all of it; this is argument assembly plus a refusal.
  - *Smallest form:* one `stage()` (~35 lines) and one `missing_env()` (~15 lines, a `grep`
    over the workdir's `.envrc`).
- **`launch` — agent config to a running `claude` process**
  - *Needs to exist:* yes, this is the spec's second and third bullets.
  - *Already solved:* almost entirely by the Claude Code CLI. `--session-id` gives us the
    handle, `--max-budget-usd` the cap, `--allowed-tools` / `--mcp-config`
    `--strict-mcp-config` the sandbox, `--add-dir` the filesystem scope, `--resume` the
    continuation. **We build an argv, not a runtime.**
  - *Smallest form:* `claude_argv(cfg, ...) -> list[str]` (~25 lines, pure, and therefore
    the most valuable `if test():` block in the repo — it is the audit of what an agent may
    do) plus `launch()` which writes `launch.json`, copies the shim, and fires one detached
    `srun`.
- **`prompts/agent_launch.md.jinja` — the launch prompt**
  - *Needs to exist:* yes. It is the mailbox contract, and it changes independently of code.
  - *Already solved:* `jinja2`, per the Code Guide's never-build-prompts-with-f-strings rule.
  - *Smallest form:* one template file, one `jinja_render` call.
- **`assets/remote_status.py` — the status shim**
  - *Needs to exist:* yes — atomicity and a fixed schema cannot be delegated to a prompt.
    It is no longer asked to solve *liveness*; the hook below does that.
  - *Already solved:* `tempfile` + `os.replace` in the stdlib.
  - *Smallest form:* ~45 lines, argparse-free (`sys.argv` positional + `--` flags), zero
    dependencies, because it runs under whatever Python the experiment repo has. Three
    verbs: `tick` (hook: refresh `updated`, recount `cells_done`), `finish` (hook: terminal
    state), and the agent's own `<state> --round … --waiting-on …`.
- **`assets/hook_settings.json.jinja` — the hooks that make liveness free**
  - *Needs to exist:* yes. It is what turns `status_stale_for` from "the agent remembered"
    into "a turn completed", and it is the review's own suggestion.
  - *Already solved:* entirely, by Claude Code. `Stop` and `SessionEnd` are existing hook
    events (verified against the installed CLI, alongside `PreToolUse`, `PostToolUse`,
    `SubagentStop`, `SessionStart`, `UserPromptSubmit`, `Notification`, `PreCompact`), and
    `--settings <path>` is the documented way to hand a session a settings file. We write
    JSON; the CLI does the rest.
  - *Smallest form:* one ~20-line template rendered into the run root. Two hook entries.
- **`prompts/job.sbatch.jinja` — batch submission**
  - *Needs to exist:* yes — Tillicum allows one interactive allocation, so anything
    overnight or parallel has to be a batch job.
  - *Already solved:* `sbatch` and jinja2. There is no new abstraction: the template emits
    `#SBATCH` directives and then the same `claude_argv` the interactive path builds.
  - *Smallest form:* one template plus `launch_batch()` (~40 lines). Batch is a submission
    difference, not a second system — staging, argv, run root, status, hooks, probe and
    `decide` are all shared.
- **`watch.decide` — the supervision rule engine**
  - *Needs to exist:* yes. "Every kill is a rule firing rather than a judgement call" is the
    spec's requirement, and a rule you can't test is a judgement call with extra steps.
  - *Already solved:* no.
  - *Smallest form:* one **pure function** `(view, thresholds, now) -> Decision`, ~30 lines
    of ordered `if`s. Pure is the point: every threshold in `supervision.yaml` gets an
    `if test():` block, with no cluster and no clock.
- **`watch.watch` — the loop**
  - *Needs to exist:* yes, but it is thin *because* `decide` is pure: poll, decide, act, log.
  - *Smallest form:* ~40 lines including the kill/renew/escalate branch.
- **`notify` — two channels, and the credential discipline around them**
  - *Needs to exist:* yes. Three callers need it (supervision escalations, the digest, and
    the agents themselves), and after the Appendix C ruling the agents send from Tillicum.
  - *Already solved:* completely, by the stdlib. Email is `smtplib` + `email.message`;
    Slack is a `urllib.request` POST of `{"text": …}` to an incoming webhook. **No
    dependency, and Slack stops being a deferred issue because it is five lines.**
  - *Smallest form:* `send_email`, `send_slack`, a `notify()` that fans out and returns
    which channels worked. Two concrete functions, no `Notifier` ABC: nothing swaps channels
    at runtime.
  - *No credentials reader, deliberately:* values come from `os.environ`, which `poe`'s
    `envfile` and the remote launch's `source .envrc` already populate. The design that
    needed a credentials parser and a bespoke secrets file was a second mechanism for no
    reason; `.envrc` plus declared key names is the one the repo already had.
  - *The part that is not code:* the human fills in `.envrc` on each machine. This repo
    writes only the placeholder template, never a value.
- **`assets/remote_notify.py` — the agent's outbound shim**
  - *Needs to exist:* yes — the compute node has the credential and no route back to the
    laptop, so this is how a 3am `needs_human` reaches a person.
  - *Already solved:* by `notify.py`, whose logic it mirrors — but the run root cannot
    import this repo (only the *experiment* repo is cloned on Tillicum), so it ships as a
    standalone zero-dependency file the way `remote_status.py` does.
  - *Smallest form:* ~40 lines. Driven by the `SessionEnd` hook off the recorded status
    block, so an agent that forgets to announce itself still announces itself; callable by
    the agent mid-run for `needs_human`.
- **`history` and `flush` — the terminal half of `poe status`**
  - *Needs to exist:* yes. `squeue` cannot answer "what completed" or "what failed"; a
    finished job leaves it within minutes, and those are half the question.
  - *Already solved:* mostly. `sacct` is SLURM's own accounting store and the run roots are
    already written for supervision — so history is a *read* of two things that exist, not
    a store we build. No database, no local index.
  - *Smallest form:* `history()` folds the probe's `sacct` rows together with the terminal
    `status.json` in each run root (~40 lines); `status_report()` groups the merged rows
    into four sections (~30, pure, so it is testable on a fixture); `flush()` deletes
    selected run roots (~30).
  - *Why `flush` deletes run roots rather than marking them read:* a local "dismissed" list
    would be laptop state that cannot be rebuilt, which is the one thing this design
    refuses. Deleting the source makes the entry leave `status` everywhere at once.
- **`monitor` — the change-gated usage digest**
  - *Needs to exist:* yes, the spec's own motivating story ("\$86 nobody noticed").
  - *Already solved:* `smtplib`/`email.message` for sending, `crontab -l | ... | crontab -`
    for the schedule (marker-delimited block, idempotent both ways — no `python-crontab`
    dependency for 25 lines of text munging).
  - *Smallest form:* `usage()` (parse `hyakusage`), `digest()` (compare against the last
    **sent** ledger row), and three cron functions.
- **`templates/envrc.example` — the file `poe init` copies**
  - *Needs to exist:* yes. "Which variables do I even need?" is exactly the question a fresh
    clone should not answer by reading source, and it is not answerable from
    `config/*.yaml` alone because the answer spans the manager and every agent config.
  - *Already solved:* the mechanism is (`.envrc` + `os.environ`); the **inventory** is not.
    This file is documentation that happens to be executable.
  - *Smallest form:* one committed file, one `KEY=<secret-here>` line per declared key with
    a one-line comment. `poe init` copies it to `.envrc` if absent and chmods 0600.
  - *Generated, not hand-maintained:* the key list comes from `ManagerConfig.requires_env`
    plus every `agents/*.yaml` `requires_env`, so the template cannot drift out of date the
    way a hand-written setup doc does. A test asserts the committed example covers exactly
    the declared union.
  - *The guardrail that makes shipping it safe:* every value in the **committed** example is
    the literal `<secret-here>`, asserted by a test — a real password reaching this file is
    the one way a copyable template turns into a leak. And `poe healthcheck` fails on a
    `.envrc` whose declared key is still `<secret-here>`, which is what turns "I copied it"
    into "I filled it in".
- **`preflight` — `poe init` and `poe healthcheck`**
  - *Needs to exist:* yes — "a fresh clone can prove it is set up", and it is where the
    spec's two unverified cluster assumptions get probed instead of discovered at 3am.
  - *Already solved:* no, but each check is 3–8 lines and they share `remote.run`.
  - *Smallest form:* a list of `(name, check_fn)` and a printer; ~70 lines for seven checks.
- **`cli` — one cyclopts app, `poe` tasks as one-line wrappers**
  - *Needs to exist:* yes — "everything above is a command in the repo".
  - *Already solved:* `cyclopts` (already a juplit dependency, and juplit's own CLI is
    built on it). Verified: a poe `cmd` task with no declared `args` forwards every trailing
    argument verbatim, so `job-up = {cmd = "slurm-agent job-up"}` makes
    `poe job-up remote_dev --gpus 2` work with no per-flag TOML.
  - *Smallest form:* one `@app.command` per capability, each a 1–3 line call into the
    modules above. `poe --help` is the inventory; `slurm-agent job-up --help` is the detail.
- **`sessions/_template.py` — the session artifact notebook**
  - *Needs to exist:* marginal, and it is the one item here I would accept being cut. Kept
    because the spec asks for every capability to be a command and because the template is
    where the **Experiment Guide**'s front-matter/round shape gets pre-loaded so a session
    notebook is right by default.
  - *Already solved:* juplit owns everything mechanical (`artifact_notebooks`, `check`,
    `normalize`, the `.gitignore` negation).
  - *Smallest form:* one template file plus `poe session-new NAME` = copy + `sed` the title.
    ~12 lines.

## 1. Pseudocode

In call order: config, the remote seam, allocations, staging, launch, supervision, monitor,
preflight.

### `slurm_agent/config.py`

```python
class ClusterConfig(BaseModel):
    """Everything Tillicum-specific. Klone is not designed for, but nothing is hard-coded."""
    model_config = ConfigDict(extra="forbid")
    login_host: str                      # ssh alias, e.g. "tillicum-login"
    node_config_path: Path               # ~/.ssh/tillicum-node-config, rewritten on job-up
    account: str | None = None
    default_qos: str = "interactive"
    gpu_usd_per_hour: float = 0.90
    run_root: str = "~/.slurm-agent/runs" # launch records, on the CLUSTER
    allocation_mode: Literal["no_shell", "tmux"] = "no_shell"


class AgentConfig(BaseModel):
    """One file per agent kind, in agents/. The audit surface: what this agent may do."""
    model_config = ConfigDict(extra="forbid")
    repo: str                            # "DeanLight/deepreasoner-baselines"
    ref: str
    workdir: str                         # path ON TILLICUM
    notebook: str                        # relative to workdir; may contain {EXP_ID}
    requires_env: list[str] = []
    skills: list[str] = []
    mcp: list[str] = []                  # names resolved against config/mcp.json
    allowed_tools: list[str] = []
    max_budget_usd: float                # -> claude --max-budget-usd. A RUNAWAY GUARD on
                                         # list-priced token spend, not a bill: under a
                                         # subscription the real limit is the plan window.
    mode: Literal["interactive", "batch"] = "interactive"
    lease: str = "04:00:00"              # interactive only: a supervision interval, not a
                                         # work estimate. Ignored in batch mode.
    max_leases: int = 4                  # interactive only
    batch_time: str = "12:00:00"         # batch only: a real deadline, never renewed
    model: str | None = None


class SupervisionConfig(BaseModel):
    """config/supervision.yaml — what 'stuck' means, written down."""
    model_config = ConfigDict(extra="forbid")
    poll_every: str = "5m"
    blocked_for: str = "15m"             # needs_env / needs_human
    status_stale_for: str = "30m"        # status block not updated while RUNNING
    no_progress_for: str = "45m"         # notebook file unchanged while RUNNING
    gpu_idle_for: str = "20m"
    renew_when_time_left: str = "10m"    # propose renewal this close to lease end
    on_kill: Literal["keep_staged"] = "keep_staged"


class MonitorConfig(BaseModel):
    """config/monitor.yaml — cadence and thresholds only.

    Channels and recipients moved to NotifyConfig, because the digest is no longer the only
    sender: the supervision loop and the remote agents use the same two channels.
    """
    model_config = ConfigDict(extra="forbid")
    every_days: int = 3
    only_if_changed: bool = True
    budget_used_pct: int = 80
    idle_gpu_hours: float = 2.0


def load(path: Path, model: type[T]) -> T:
    """Parse one YAML file into one pydantic model. The only config entry point."""
    # yaml.safe_load(path.read_text())
    # model.model_validate(data)  -- extra="forbid" turns a typo into a load-time error
    raise NotImplementedError


class ManagerConfig(BaseModel):
    """config/manager.yaml — the local session's own settings. The mirror of AgentConfig.

    `requires_env` names the environment KEYS the local manager needs and never their
    values, exactly as AgentConfig.requires_env does for a remote agent. Values live in the
    gitignored .envrc; committed config stays reviewable and secret-free.
    """
    model_config = ConfigDict(extra="forbid")
    requires_env: list[str] = []      # e.g. SLURM_AGENT_SMTP_PASSWORD, SLURM_AGENT_SLACK_WEBHOOK
    envrc: Path = Path(".envrc")      # where those keys are expected to be defined


def duration_seconds(text: str) -> int:
    """'5m' / '04:00:00' / '90s' -> seconds. Both SLURM and threshold spellings."""
    raise NotImplementedError


def config_errors():
    """Error cases for config."""
    # file missing                  -> FileNotFoundError naming the path and `poe init`
    # unknown key                   -> pydantic ValidationError, unmodified (it names the key)
    # AgentConfig.mcp name not in config/mcp.json -> ValueError listing the known names
    # duration_seconds unparseable  -> ValueError("bad duration 'four hours'")
    raise NotImplementedError
```

### `slurm_agent/remote.py` — the single seam

```python
Runner = Callable[[str], str]
"""Run one shell command on the login node, return stdout. The seam every test injects at."""


def ssh_runner(host: str, *, timeout: int = 60) -> Runner:
    """Build a Runner that shells out over the shared ControlMaster connection.

    Not paramiko: the ControlMaster socket is what makes UW 2FA a once-per-day event.
    """
    # return a closure that runs ["ssh", host, command] via subprocess.run(capture_output=True)
    # non-zero exit -> raise RemoteError(command, returncode, stderr)
    raise NotImplementedError


def probe(run: Runner, run_root: str) -> dict:
    """ONE round trip: the whole cluster-side world as one JSON object.

    Pipes assets/probe.sh to `sh -s` on the login node. The script emits
    {"now":…, "jobs":[…squeue rows…], "finished":[…sacct rows…],
    "runs":[{launch…, "status":{…}, "nb_mtime":…, "nb_bytes":…, "gpu_util":…}]}.
    Everything the supervision loop AND `poe status` need, one read, independent of how many
    agents are in flight. `sacct` is what makes "what completed / what failed" answerable
    after a job has left the queue, and folding it in here is what keeps `poe status` cheap
    enough to poll.
    """
    # cmd = f"sh -s {shlex.quote(run_root)}"
    # feed assets/probe.sh on stdin; parse stdout as JSON
    raise NotImplementedError


def remote_errors():
    """Error cases for remote."""
    # ssh exits non-zero              -> RemoteError with stderr attached (never swallowed)
    # ssh times out                   -> RemoteError("timed out after Ns"); a poll skips a cycle
    # probe stdout is not valid JSON  -> RemoteError with the first 500 chars of stdout,
    #                                    because a login-node MOTD prepended to the JSON is
    #                                    the likely cause and the message should show it
    raise NotImplementedError
```

### `slurm_agent/jobs.py` — allocations

```python
class Job(BaseModel):
    """One allocation, as squeue sees it."""
    model_config = ConfigDict(extra="forbid")
    name: str
    job_id: str
    node: str | None
    state: str                    # RUNNING / PENDING / …
    time_left_s: int | None
    gpus: int
    elapsed_s: int

    @property
    def gpu_usd(self) -> float: ...   # gpus * elapsed_h * cluster.gpu_usd_per_hour


def job_list(run: Runner) -> list[Job]:
    """Every allocation of mine. One squeue call with an explicit --Format."""
    # squeue --me --noheader --Format=Name,JobID,NodeList,State,TimeLeft,TimeUsed,tres-alloc
    # parse each row into a Job; gpus read out of the tres string
    raise NotImplementedError


def job_up(name: str, run: Runner, cluster: ClusterConfig, *, gpus: int, time: str,
           qos: str | None = None, cpus: int = 8, mem: str = "200G",
           wait_s: int = 300) -> Job:
    """Bring up an allocation named `name`, or return the one already running under it.

    salloc --no-shell so the allocation does NOT die with the ssh connection that asked
    for it (spec Q&A: a closed laptop must not take the agent's job with it). Falls back
    to a tmux holder on the login node when cluster.allocation_mode says so.

    Tillicum permits ONE interactive allocation, so this never submits a second: an
    existing interactive allocation under any name is reported and returned. Agents share
    it as --overlap job steps; anything that needs its own job goes to batch.
    """
    # existing = first job_list() row named `name` in RUNNING or PENDING -> return it
    # any OTHER interactive allocation of mine is RUNNING/PENDING -> return it with a
    #     warning naming it; never submit a second (it would queue behind my own job)
    # build: salloc --no-shell --job-name=NAME --gpus=N --cpus-per-task=C --mem=M
    #        --time=T [--qos=Q] [--account=A]
    # allocation_mode == "tmux" -> wrap as: tmux new-session -d -s NAME 'salloc … '
    # run it, then poll job_list every 5s until RUNNING or wait_s elapses
    # on RUNNING: update_node_config(...) so vscode/ssh follow the node
    raise NotImplementedError


def job_down(name: str, run: Runner) -> str:
    """scancel the allocation named `name`. Returns the cancelled job id."""
    raise NotImplementedError


def job_shell_command(name: str, run: Runner, cluster: ClusterConfig) -> list[str]:
    """The argv for an interactive shell ON the compute node. Returned, then os.execvp'd.

    Ported from slurm-ops, which printed it for a human to paste; here the CLI runs it.
    """
    # ["ssh", "-t", host, f"srun --jobid={jid} --overlap --pty bash -l"]
    raise NotImplementedError


def update_node_config(node: str, config_path: Path) -> None:
    """Rewrite the Hostname line in ~/.ssh/<cluster>-node-config. Ported from slurm-ops."""
    # re.sub(r"(?m)^(\s*Hostname)\s+\S+", rf"\1 {node}", text)
    raise NotImplementedError


def job_errors():
    """Error cases for jobs."""
    # no job named `name`, job_down/job_shell -> LookupError naming `name` and listing mine
    # salloc rejects --no-shell               -> RemoteError; message points at
    #                                            allocation_mode: tmux and `poe init`
    # allocation still PENDING after wait_s   -> return the PENDING Job with a warning,
    #                                            never raise: a queued job is not a failure
    # a different interactive allocation exists -> returned with a warning, not an error;
    #                                            the cap is a fact to work with, not a fault
    # node_config_path missing                -> FileNotFoundError naming `poe init`
```

### `slurm_agent/staging.py`

```python
def stage(agent: AgentConfig, run: Runner) -> str:
    """Clone or fetch agent.repo at agent.ref into agent.workdir on Tillicum. Returns the SHA.

    Refuses a dirty tree. Tillicum holds many repos; a launch onto the wrong or modified
    one is how an experiment silently measures code nobody reviewed.
    """
    # if workdir absent            -> git clone --branch REF REPO WORKDIR
    # else                         -> git -C W fetch origin REF
    #                                 git -C W status --porcelain  -> must be empty
    #                                 git -C W checkout --detach FETCH_HEAD
    # return git -C W rev-parse HEAD
    raise NotImplementedError


def missing_env(agent: AgentConfig, run: Runner) -> list[str]:
    """Which of agent.requires_env are absent from the workdir's .envrc. Names, not values.

    Checked BEFORE launch, so a missing token costs nothing instead of a GPU-hour. This
    repo never writes secrets to the shared filesystem (spec: out of scope) — it only
    reports which names a human must add.
    """
    # grep -E '^(export[[:space:]]+)?(A|B|C)=' WORKDIR/.envrc  -> the names that matched
    # return the requested names minus the matched ones (a missing .envrc means all of them)
    raise NotImplementedError


def staging_errors():
    """Error cases for staging."""
    # dirty tree        -> DirtyWorkdirError listing the modified paths; never auto-stash
    # unknown ref       -> RemoteError from git, unmodified (git's message is the good one)
    # clone into a path that exists but is not a git repo -> ValueError naming the path
    # workdir is not under $HOME or a configured scratch root -> ValueError (refuse to
    #                                                            clone into someone else's tree)
```

### `slurm_agent/launch.py`

```python
def claude_argv(agent: AgentConfig, *, prompt: str, session_id: str,
                mcp_config_path: str | None, settings_path: str,
                resume: bool = False) -> list[str]:
    """Build the exact `claude` command line. Pure — this is the audit of what an agent may do.

    Deliberately NOT --bare, for three independent reasons: it restricts Anthropic auth to
    ANTHROPIC_API_KEY and never reads OAuth (the Tillicum credential is the user's
    authenticated subscription, per the spec's Q&A); it skips hooks, which is where
    liveness comes from; and it skips CLAUDE.md discovery, which is how the staged repo
    tells the agent its own conventions.
    """
    # ["claude", "-p", prompt,
    #  "--session-id", session_id,          # WE choose it, so it is the handle everywhere
    #  "--output-format", "json",
    #  "--permission-mode", "dontAsk",      # no interactive prompt can ever block the run
    #  "--max-budget-usd", str(agent.max_budget_usd),
    #  "--add-dir", agent.workdir,
    #  "--settings", settings_path,        # the Stop / SessionEnd hooks; see prepare_run
    #  "--allowed-tools", *agent.allowed_tools,
    #  "--mcp-config", mcp_config_path, "--strict-mcp-config",   # if agent.mcp
    #  "--model", agent.model]              # if set
    # resume -> ["claude", "-p", prompt, "--resume", session_id, …the same flags]
    raise NotImplementedError


def launch_prompt(agent: AgentConfig, *, task: str, notebook: str, run_dir: str) -> str:
    """Render prompts/agent_launch.md.jinja: the task, the notebook, the mailbox contract.

    Tells the agent: read the Dev Workspace through the Notion MCP; this notebook is your
    experiment log, written round by round; report state with
    `python RUN_DIR/remote_status.py <state> --round … --waiting-on …` after every round.
    """
    raise NotImplementedError


def launch(agent_name: str, task: str, job_name: str, run: Runner,
           cluster: ClusterConfig, *, exp_id: str | None = None) -> str:
    """Stage, preflight, and start a Claude agent on the allocation. Returns the session id.

    Refuses rather than launching when anything is not ready — every refusal here is a
    GPU-hour not spent.
    """
    # agent = load(agents/<agent_name>.yaml, AgentConfig)
    # job   = the RUNNING allocation named job_name, else LookupError
    # spare GPUs on `job` >= what this agent needs, else ContentionError naming the
    #     agents already on it -- one allocation is shared by everyone (see Two execution modes)
    # session_id, run_dir = prepare_run(agent, task, run, cluster, exp_id)
    # argv = claude_argv(agent, prompt=…, session_id=session_id, settings=run_dir/settings.json)
    # fire DETACHED so closing the laptop cannot kill it:
    #   setsid nohup srun --jobid=JID --overlap --chdir=WORKDIR
    #       --output=RUN_DIR/agent.log --error=RUN_DIR/agent.err
    #       bash -lc 'source .envrc 2>/dev/null; export SLURM_AGENT_RUN_DIR=RUN_DIR;
    #                 exec <argv>' </dev/null &
    # return session_id
    raise NotImplementedError


def prepare_run(agent: AgentConfig, task: str, run: Runner, cluster: ClusterConfig,
                exp_id: str | None) -> tuple[str, str]:
    """Stage, preflight, and lay out the run root. Shared by interactive and batch launch.

    Everything this repo writes goes in the run root and NOTHING goes inside the staged
    repo -- otherwise stage()'s dirty-tree refusal, which is what stops an experiment
    measuring unreviewed code, would fire on our own leftovers.
    """
    # sha  = stage(agent, run)
    # miss = missing_env(agent, run) -> if miss: raise MissingEnvError(miss, agent.workdir)
    # session_id = str(uuid4()); run_dir = f"{cluster.run_root}/{session_id}"; mkdir -p
    # write, all by heredoc over ssh (no scp round trip):
    #     run_dir/remote_status.py   <- assets/remote_status.py
    #     run_dir/remote_notify.py   <- assets/remote_notify.py
    #     run_dir/settings.json      <- render assets/hook_settings.json.jinja:
    #           Stop       -> python RUN_DIR/remote_status.py tick --notebook <abs .ipynb>
    #           SessionEnd -> python RUN_DIR/remote_status.py finish --notebook <abs .ipynb>
    #                      && python RUN_DIR/remote_notify.py --from-status
    #                         (derives subject/body from the block just written, so the
    #                          message reports recorded state rather than improvisation;
    #                          records the delivered channels back into the block, which is
    #                          what `announced` reads)
    #     run_dir/launch.json        <- session_id, task, agent kind, mode, repo, ref, sha,
    #           workdir, notebook (abs, {EXP_ID} substituted), job_id/job_name (interactive)
    #           lease, max_leases, leases_used=1, max_budget_usd, launched_at
    # return session_id, run_dir
    raise NotImplementedError


def launch_batch(agent_name: str, task: str, run: Runner, cluster: ClusterConfig,
                 *, exp_id: str | None = None, time: str | None = None) -> str:
    """Submit the same agent as an sbatch job. Returns the session id.

    For overnight and parallel work: Tillicum caps interactive allocations at one, batch
    jobs are not capped, and a batch job ENDS WHEN THE AGENT EXITS because the claude run
    is the script's last statement. Self-termination is structural here, not a promise the
    agent has to keep.
    """
    # session_id, run_dir = prepare_run(...)   # identical to interactive
    # render prompts/job.sbatch.jinja -> run_dir/job.sbatch:
    #     #SBATCH --job-name/--gpus/--time/--account/--output=RUN_DIR/agent.log
    #     cd WORKDIR; source .envrc; export SLURM_AGENT_RUN_DIR=RUN_DIR
    #     exec <claude_argv>          <- the script's LAST statement, hence self-ending
    # job_id = run(f"sbatch --parsable {run_dir}/job.sbatch")
    # record job_id + mode="batch" into launch.json; return session_id
    raise NotImplementedError


def continue_run(session_id: str, job_name: str, run: Runner, cluster: ClusterConfig) -> str:
    """A fresh lease on the same notebook: same session, same workdir, --resume.

    Cheap because the run cells are idempotent (Experiment Guide): finished rounds skip on
    their DONE markers and the run picks up where the kill stopped. Nothing is re-staged.
    """
    # read run_dir/launch.json; refuse if leases_used >= max_leases (escalate instead)
    # bump leases_used, rewrite launch.json, re-fire the same detached srun with resume=True
    raise NotImplementedError


def launch_errors():
    """Error cases for launch."""
    # missing env vars        -> MissingEnvError listing names + the .envrc path to fix.
    #                            NOTHING is launched. This is the cheap version of needs_env.
    # no spare GPUs on the shared allocation -> ContentionError naming the agents already
    #                            on it, and suggesting agent-batch instead
    # dirty / missing workdir -> propagates from stage(); nothing launched
    # job_name not RUNNING    -> LookupError naming `poe job-up`
    # leases_used >= max      -> LeaseExhausted; continue_run escalates to a human instead
    # agent.mcp names a server not in config/mcp.json -> ValueError at config load, pre-launch
```

### `slurm_agent/watch.py` — supervision

```python
class AgentView(BaseModel):
    """One remote agent, merged from launch.json + its status block + squeue + the file system.

    Two progress signals on purpose: `status_age_s` is what the agent SAYS about itself,
    `notebook_age_s` is what the file system OBSERVED. A stuck agent fails one or the other.
    """
    model_config = ConfigDict(extra="forbid")
    session_id: str
    task: str
    mode: Literal["interactive", "batch"]
    job_id: str
    job_state: Literal["RUNNING", "PENDING", "TIMEOUT", "FAILED", "GONE"]
    time_left_s: int | None
    state: Literal["running", "needs_env", "needs_human", "finished", "failed", "unknown"]
    round: str | None
    waiting_on: list[str]
    cells_done: int | None
    status_age_s: int | None       # self-reported freshness
    notebook_age_s: int | None     # observed freshness (the .ipynb mtime)
    gpu_idle_s: int | None
    gpu_usd: float
    agent_usd: float | None        # None until the run exits; --max-budget-usd caps it meanwhile
    leases_used: int
    max_leases: int
    announced: bool                # did the agent post its own completion to GitHub?


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["watch", "kill", "renew", "escalate", "done"]
    rule: str | None               # the threshold that fired, e.g. "blocked_for=15m"
    detail: str


def views(snapshot: dict, cluster: ClusterConfig, now: float) -> list[AgentView]:
    """Turn one probe() blob into one row per agent. Pure: no I/O, so it is testable on a fixture."""
    raise NotImplementedError


def decide(view: AgentView, rules: SupervisionConfig, now: float) -> Decision:
    """The whole supervision policy, as a pure function. Every kill names the rule that fired.

    Order matters and is the policy: terminal states first, then human-blocked (stop paying
    immediately — nobody is appearing in the next four hours), then the three staleness
    rules, then lease renewal, then keep watching.
    """
    # state == "finished" and announced
    #                      -> Decision("done", None, "finished; the agent announced itself")
    #        The agent posts its own completion to its PR (it already has the git
    #        credential and the GitHub MCP). A success that announced itself needs no
    #        second message -- this is what keeps the human's interrupts down to failures.
    # state == "finished" and not announced
    #                      -> Decision("escalate", None, "finished but never announced")
    # state == "failed"    -> Decision("escalate", None, "run failed")
    # job_state == "TIMEOUT" and state != "finished"      # batch hit its walltime
    #                      -> Decision("escalate", None, "batch job timed out unfinished")
    # job_state in ("GONE","FAILED") and state == "running"
    #                      -> Decision("escalate", None, "job vanished mid-run")
    # state in ("needs_env","needs_human") and blocked longer than rules.blocked_for
    #                      -> Decision("kill", "blocked_for", "waiting on {waiting_on}")
    # status_age_s  > rules.status_stale_for   -> kill, "status_stale_for"
    # notebook_age_s> rules.no_progress_for    -> kill, "no_progress_for"
    # gpu_idle_s    > rules.gpu_idle_for       -> kill, "gpu_idle_for"
    # mode == "interactive" and time_left_s < rules.renew_when_time_left:
    #       leases_used < max_leases -> Decision("renew", None, "lease ending")   # PROPOSED
    #       else                     -> Decision("escalate", None, "lease budget exhausted")
    #    Batch never renews: its walltime is a real deadline, not a supervision interval,
    #    so a batch job near its end is watched and its TIMEOUT is handled above.
    # otherwise            -> Decision("watch", None, "round {round}, {time_left} left")
    raise NotImplementedError


def act(decision: Decision, view: AgentView, run: Runner, cluster: ClusterConfig,
        *, auto_renew: bool = False) -> str:
    """Carry out a decision and return the one line that goes in the session notebook.

    Rules kill; the MANAGER renews. `renew` is surfaced, not taken, unless --auto-renew:
    the spec is explicit that renewal happens only after somebody reads the status block,
    the log and the notebook, and that is a judgement a threshold cannot make.
    """
    # kill     -> interactive: scancel THIS AGENT'S STEP, not the allocation -- the
    #             allocation is shared and its other agents must survive. The allocation
    #             itself is only cancelled when it holds no live steps.
    #             batch: scancel the job (it is this agent's alone).
    #             Either way record the rule, the state at the time and the estimated
    #             saving; keep_staged means nothing on disk is touched.
    # escalate -> notify.send(...) once per (session_id, reason); never twice for the same thing
    # renew    -> auto_renew ? (job_up + continue_run) : print the proposal and keep watching
    # watch    -> nothing
    raise NotImplementedError


def watch(run: Runner, cluster: ClusterConfig, rules: SupervisionConfig, *,
          once: bool = False, auto_renew: bool = False) -> None:
    """poll -> decide -> act -> log, forever. Safe to kill and restart: it holds no state.

    A closed laptop kills this loop and loses nothing, because every fact it works from is
    re-derived from the next probe.
    """
    # while True:
    #     try: snapshot = probe(run, cluster.run_root)
    #     except RemoteError as e: log.warning(...); sleep; continue   # one skipped cycle
    #     for v in views(snapshot, cluster, now): print(act(decide(v, rules, now), v, …))
    #     if once: return
    #     sleep(rules.poll_every)
    raise NotImplementedError


def kill(session_id: str, run: Runner, cluster: ClusterConfig, *, reason: str) -> str:
    """Manual kill with a human's reason, recorded the same way an automatic one is."""
    raise NotImplementedError


class HistoryRow(BaseModel):
    """One finished run, reconstructed after its job left the queue."""
    model_config = ConfigDict(extra="forbid")
    session_id: str
    task: str
    mode: Literal["interactive", "batch"]
    job_id: str
    job_state: str                 # COMPLETED / FAILED / TIMEOUT / CANCELLED, from sacct
    state: str                     # the agent's own terminal state, from status.json
    ended_at: float | None
    elapsed_s: int | None
    gpu_usd: float
    agent_usd: float | None        # known here, because the run has exited
    killed_by_rule: str | None


def history(snapshot: dict, cluster: ClusterConfig) -> list[HistoryRow]:
    """Every run that has finished. Pure: folds the probe's sacct rows into the run roots.

    The half of `poe status` that squeue cannot answer. Both sources are on the CLUSTER --
    sacct's accounting store and the run roots the SessionEnd hook wrote -- so history costs
    no laptop state and two sessions still agree.
    """
    # for each run root with a TERMINAL status.json: join it to its sacct row by job_id
    # a run root whose job sacct no longer knows -> job_state="UNKNOWN", still listed
    # an sacct row with no run root (flushed) -> skipped; flushing means forgetting
    raise NotImplementedError


def status_report(views: list[AgentView], history: list[HistoryRow],
                  jobs: list[Job]) -> str:
    """The four sections of `poe status`: running, queued, completed, failed. Pure.

    The between-sessions poll: one screen, one round trip. Grouping is by observed state,
    not by what the agent claims -- a run whose agent says `finished` but whose job says
    FAILED is listed under failed, because the job is the harder fact.
    """
    raise NotImplementedError


def flush(run: Runner, cluster: ClusterConfig, *, older_than: str = "7d",
          failed: bool = False, session_id: str | None = None,
          dry_run: bool = False) -> list[str]:
    """Delete finished run roots so they leave `status`. Returns what was removed.

    Timid on purpose. Completed runs only unless --failed: a success's evidence is already
    in its committed notebook and its PR, but `agent.err` is the ONLY record of why a failed
    agent died, and the spec requires that question stay answerable. Never touches a live
    session, and never anything inside a staged repo -- only ~/.slurm-agent/runs/<id>/.
    """
    # refuse any session whose status.json is non-terminal OR whose job is live in squeue
    # select terminal roots older than `older_than`; without --failed, state=="finished" only
    # dry_run -> return the list without deleting
    raise NotImplementedError


def watch_errors():
    """Error cases for watch."""
    # status block is unreadable/half-written -> state="unknown", status_age_s from mtime.
    #     Never crashes the loop: the atomic write in the shim makes this rare, and a rare
    #     unreadable block must degrade to "stale", not take supervision down.
    # launch.json references a notebook that does not exist -> notebook_age_s=None, and
    #     no_progress_for cannot fire (an absent file is not evidence of a stuck agent)
    # `announced` is unreadable (GitHub unreachable) -> treat as NOT announced and escalate.
    #     Erring toward one extra message beats silently dropping a finished run.
    # probe raises            -> log, skip the cycle, keep the loop alive
    # kill target not found   -> LookupError naming the session ids that are live
```

### `slurm_agent/monitor.py`

```python
def usage(run: Runner) -> dict:
    """Run hyakusage on the login node and parse it into {account: {used, limit, unit}}.

    The parser is written against a captured sample committed as a fixture — see
    Appendix A; the format is the one thing here nobody has verified from a real cluster.
    """
    raise NotImplementedError


def digest(current: dict, ledger_path: Path, cfg: MonitorConfig,
           batch: list[dict] | None = None) -> str | None:
    """The message to send, or None when there is nothing to say.

    `batch` is the sacct rollup of batch jobs that finished since the last digest -- the
    review asked for the scheduled report to cover how overnight jobs went. It REPORTS
    them; the scheduled monitor still never acts on the cluster (spec, Out of scope). Kill
    authority stays with the manager alone.

    Silence has to mean 'nothing changed', so a message always means something did. The
    comparison is against the last row that was actually SENT, not the last row observed —
    otherwise three unsent polls in a row hide a change that happened across them.
    """
    # last_sent = last ledger row with sent=True
    # unchanged and cfg.only_if_changed -> None
    # else render: the delta since last_sent, plus any alert_when threshold crossed
    raise NotImplementedError


def monitor_run(run: Runner, cfg: MonitorConfig, ledger_path: Path,
                *, dry_run: bool = False) -> str:
    """The scheduled entry point: poll, append to the ledger, send only if there is news."""
    # append {observed_at, usage, sent=False} to the ledger (JSONL: append-only, no db)
    # body = digest(...); if None -> print 'spend unchanged since <date> — nothing to send'
    # dry_run -> print the body; else notify.send(...) then mark the row sent=True
    raise NotImplementedError


def cron_write(block: str | None, *, repo_root: Path) -> str:
    """Install (block=text), or remove (block=None), ONE marker-delimited crontab entry.

    Both directions idempotent, which is what makes them safe to re-run:
        # >>> slurm-agent monitor >>>
        0 9 */3 * * cd <repo_root> && uv run slurm-agent monitor-run
        # <<< slurm-agent monitor <<<
    Read with `crontab -l`, replace the marked block, write back with `crontab -`. The
    markers are why this needs no dependency and never touches a line it did not write.
    """
    raise NotImplementedError


def cron_status() -> str:
    """Installed or not, when it last ran, when the last digest was sent, when it fires next.

    'Last ran' comes from the ledger's newest row and 'last sent' from its newest sent row,
    so the status line is derived from the same file the digest is — never a second record.
    """
    raise NotImplementedError


def monitor_errors():
    """Error cases for monitor."""
    # hyakusage absent / output unparseable -> ValueError with the raw first lines, so the
    #     fixture can be updated from the message alone
    # crontab unavailable (no cron on this machine) -> RuntimeError naming launchd/systemd
    #     as the manual alternative; never silently no-op
    # SMTP failure -> log at error and re-raise. A digest that silently fails to send is
    #     indistinguishable from 'nothing changed', which is the one thing this must not be.
```

### `slurm_agent/notify.py`

Used by three callers — the supervision loop's escalations, the scheduled digest, and (via
the shim) the remote agents themselves. One module, two sibling functions, no `Notifier`
ABC: two concrete channels that never need to be swapped at runtime do not justify one.

```python
class NotifyConfig(BaseModel):
    """config/notify.yaml — channels and recipients. No secrets and no paths to secrets."""
    model_config = ConfigDict(extra="forbid")
    channels: list[Literal["email", "slack"]] = ["email"]
    to: str | None = None                # email recipient
    slack_channel: str | None = None     # optional override; the webhook has a default


# There is NO credentials reader in this design. Values arrive in os.environ: `poe` loads
# .envrc for every task via [tool.poe] envfile, and the remote launch does
# `bash -lc 'source .envrc; exec …'`. Writing a parser would have been a second secrets
# mechanism competing with the one the repo already uses for every experiment repo.


def send_email(subject: str, body: str, *, to: str, secrets: dict[str, str]) -> None:
    """One email via smtplib + email.message. STARTTLS, app-password auth."""
    # EmailMessage(); set Subject/From/To; set_content(body)
    # smtplib.SMTP(host, port) -> starttls() -> login(user, password) -> send_message()
    raise NotImplementedError


def send_slack(text: str, *, secrets: dict[str, str], channel: str | None = None) -> None:
    """One Slack message, POSTed to an incoming webhook with urllib.request.

    A webhook is a URL that takes {"text": …}. No SDK, no OAuth app, no dependency —
    which is why Slack stops being 'a repo issue' and becomes five lines.
    """
    raise NotImplementedError


def notify(subject: str, body: str, cfg: NotifyConfig) -> list[str]:
    """Send on every configured channel. Returns the channels that succeeded.

    Never raises on a partial failure: one channel down must not suppress the other, and a
    caller that is reporting a crash should not itself crash. It returns what got through
    so the caller can record 'announced on slack, email failed' rather than guessing.
    """
    raise NotImplementedError


def scrub(text: str, keys: list[str]) -> str:
    """Replace the value of every declared key with '***' wherever it appears in `text`.

    Applied to every message that leaves this module. A bad password echoed by an SMTP
    server must not reach a log, a status block or a committed notebook -- and `keys` is
    exactly ManagerConfig/AgentConfig.requires_env, so the scrubber knows what is secret
    for the same reason the checks do.
    """
    raise NotImplementedError


def notify_test(cfg: NotifyConfig, run: Runner | None = None) -> list[Check]:
    """Really send one message per channel, from here and (with `run`) from Tillicum.

    A real delivery is the only thing that proves the pipeline, which is why `poe init`
    calls this rather than inferring reachability. Standalone as `poe notify-test` for
    re-checking after a password rotation.
    """
    raise NotImplementedError


def notify_errors():
    """Error cases for notify."""
    # a declared key is unset     -> MissingEnvError naming the KEYS and the .envrc to fix,
    #                                the same error staging.missing_env raises for an agent
    # a declared key still reads '<secret-here>'
    #                             -> MissingEnvError too: copying the template is not
    #                                filling it in, and the two must not look alike
    # SMTP auth rejected          -> NotifyError with the server's message, run through
    #                                scrub() (a bad password must not be echoed into a log,
    #                                a status block or a notebook)
    # webhook returns non-2xx     -> NotifyError with status and the first 200 chars
    # one channel up, one down    -> no raise; notify() returns the channel that worked
    # every channel down          -> NotifyError; watch.act logs it and keeps the loop alive
```

### `slurm_agent/preflight.py` — `poe init` and `poe healthcheck`

Two commands, because they answer two different questions at two different rhythms.
**`init` creates**: run once, when wiring a fresh clone to the cluster. **`healthcheck`
verifies**: run whenever you have moved network, re-authed to Tillicum, or want to know
whether anything is wired wrong — so it has to be fast, and `poe hc` is the alias that
makes it worth typing.

```python
class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    ok: bool | None                 # None = SKIPPED. A skipped proof is not a proof.
    detail: str
    fix: str | None = None


def init(cluster: ClusterConfig, manager: ManagerConfig, agents: list[AgentConfig],
         *, send: bool = True) -> list[Check]:
    """Create the local footprint, then hand off to a FULL healthcheck.

    Creates only what is safe to create and never a secret value:
      - git hooks               `pre-commit install`
      - ~/.ssh entries          from ssh_config_templates/, if absent
      - .envrc                  from templates/envrc.example, chmod 0600, if absent --
                                every declared key present with a `<secret-here>` value,
                                so the file is complete and obviously unfinished
      - cluster.run_root        mkdir -p over ssh
    Then returns healthcheck(full=True, send=send). Setting a clone up and never proving
    the notifications work is how you find out at 3am, so init sends by default.
    """
    raise NotImplementedError


def healthcheck(cluster: ClusterConfig, manager: ManagerConfig, notify_cfg: NotifyConfig,
                run: Runner, *, full: bool = False, send: bool = False) -> list[Check]:
    """Is everything wired and working? FAST tier by default; `--full` adds the slow proofs.

    The fast tier is the one you run after moving desks: seconds, no tokens, no GPU, no
    messages sent. The full tier is what `init` runs and what you run before trusting an
    overnight batch job.
    """
    # ---- FAST (always) -- seconds, free, no side effects ------------------------
    # git hooks         : pre-commit installed
    # ssh config        : login + node hosts present in ~/.ssh/config
    # cluster identity  : `ssh HOST id -un` returns the expected user; ControlMaster live.
    #                     THE reason `hc` exists -- a dropped ControlMaster after a network
    #                     change is the single most common "everything is broken" cause,
    #                     and it is a one-second check.
    # .envrc present    : the file exists and is mode 0600 (local, and on the cluster)
    # declared env      : missing_env(manager.requires_env) is empty, AND no declared key
    #                     still reads `<secret-here>`. Reports KEY NAMES only, never values.
    # agent env         : same for each agents/*.yaml requires_env, against its workdir
    # run root          : cluster.run_root exists on the cluster
    # node config       : ~/.ssh/<cluster>-node-config points at a node that is still mine
    # usage monitor     : cron block installed (report only -- never installs silently)
    #
    # ---- FULL (--full; what `init` runs) -- slow, costs tokens or a queue slot ----
    # ALLOCATION PROBE  : `salloc --no-shell --time=00:01:00 …` then scancel -- does the
    #                     allocation outlive the ssh that asked for it? Sets/echoes
    #                     allocation_mode, and reports whether a SECOND interactive
    #                     allocation is refused. VERIFIES the biggest lease assumption.
    # AGENT CREDENTIAL  : `ssh HOST 'claude -p "reply OK" --output-format json'` -- proves
    #                     the subscription auth works headlessly before any GPU spend, AND
    #                     asserts total_cost_usd > 0: a subscription reporting zero would
    #                     silently disarm --max-budget-usd
    # BATCH             : `sbatch --test-only` on the rendered template -- the partition
    #                     accepts our walltime, checked without queueing anything
    #
    # ---- SEND (--send; on by default from `init`) --------------------------------
    # NOTIFY SEND (local)   : really send one message per configured channel, from here
    # NOTIFY SEND (cluster) : really send one per channel, from Tillicum over ssh. The row
    #                     nothing else can stand in for: a DIFFERENT egress path, and the
    #                     one every agent depends on. Settles the Appendix A question of
    #                     whether compute-side egress reaches port 587 / hooks.slack.com.
    # Without --send both rows report ok=None (SKIPPED), never ok=True.
    raise NotImplementedError


def render(checks: list[Check]) -> str:
    """The spec's report: name, ok/MISSING/SKIPPED, detail. Exit non-zero if any failed.

    A SKIPPED row renders as SKIPPED and does not fail the run, but never renders as ok --
    the whole point of the send rows is that only a delivered message proves delivery.
    """
    raise NotImplementedError


def preflight_errors():
    """Error cases for preflight."""
    # .envrc missing            -> ok=False, fix: `poe init` (which creates it)
    # .envrc mode 0644          -> ok=False naming the path and `chmod 600`. On a shared
    #                              filesystem this is the real exposure, so it FAILS.
    # declared key unset or still `<secret-here>`
    #                           -> ok=False listing the KEY NAMES and the .envrc to edit.
    #                              Never prints a value, not even a partial one.
    # ssh unreachable           -> the identity row fails and every cluster-side row is
    #                              SKIPPED rather than failed: one broken link must not
    #                              render as eight independent problems.
    raise NotImplementedError
```

### `slurm_agent/cli.py`

```python
app = cyclopts.App(name="slurm-agent")

@app.command
def init_cmd(send: bool = True) -> None: ...          # create, then a full healthcheck
@app.command
def healthcheck(full: bool = False, send: bool = False) -> None: ...   # `poe hc` aliases this
@app.command(name="notify-test")
def notify_test_cmd() -> None: ...      # really sends, from both machines
@app.command(name="job-up")
def job_up_cmd(name: str, gpus: int = 1, time: str = "04:00:00",
               qos: str | None = None, cpus: int = 8, mem: str = "200G") -> None: ...
@app.command(name="job-status")
def job_status_cmd() -> None: ...
@app.command(name="job-shell")
def job_shell_cmd(name: str) -> None: ...        # os.execvp — becomes the shell
@app.command(name="job-down")
def job_down_cmd(name: str) -> None: ...
@app.command(name="agent-run")
def agent_run_cmd(task: str, job: str, agent: str, exp_id: str | None = None) -> None: ...
@app.command(name="agent-batch")
def agent_batch_cmd(task: str, agent: str, time: str | None = None,
                    exp_id: str | None = None) -> None: ...
@app.command
def status(older_than: str = "14d") -> None: ...   # running / queued / completed / failed
@app.command
def flush(older_than: str = "7d", failed: bool = False, session: str | None = None,
          dry_run: bool = False) -> None: ...
@app.command(name="agent-status")
def agent_status_cmd() -> None: ...
@app.command(name="agent-logs")
def agent_logs_cmd(session: str, cells: bool = False, tail: int = 50) -> None: ...
@app.command(name="agent-watch")
def agent_watch_cmd(once: bool = False, auto_renew: bool = False) -> None: ...
@app.command(name="agent-kill")
def agent_kill_cmd(session: str, reason: str) -> None: ...
@app.command(name="agent-continue")
def agent_continue_cmd(session: str, job: str | None = None) -> None: ...
@app.command(name="monitor-run")
def monitor_run_cmd(dry_run: bool = False) -> None: ...
@app.command(name="monitor-install")
def monitor_install_cmd() -> None: ...
@app.command(name="monitor-status")
def monitor_status_cmd() -> None: ...
@app.command(name="monitor-uninstall")
def monitor_uninstall_cmd() -> None: ...
@app.command(name="session-new")
def session_new_cmd(name: str) -> None: ...
```

`agent-logs --cells` shells out to `juplit cells` **on the login node** over the shared
filesystem, so a 4 MB notebook costs a few hundred tokens to inspect and is never copied
back to the laptop.

## 2. Libraries and dependencies

The repo is greenfield, so everything is technically new. What matters is that nothing here
is exotic and nothing is written that a dependency already does.

**Already standard in this org's repos** (the same set `deep_reasoner`, `juplit` and
`generative_circuits` use — no new ground):

- `juplit` — the notebook workflow, artifact notebooks, `check` / `cells` / `view` /
  `normalize`. **Owns everything about the session notebook and the remote agent's log.**
  This repo writes no notebook code.
- `cyclopts` — the CLI. Already a transitive dependency via juplit, and juplit's own CLI is
  built on it, so the two behave identically.
- `poethepoet` — the command inventory. Verified above that `cmd` tasks with no declared
  `args` forward trailing arguments verbatim, which is what lets every `poe` task be a
  one-line passthrough instead of a per-flag TOML re-declaration.
- `pydantic` — every config file and every wire shape (`Job`, `AgentView`, `Decision`,
  `Check`), with `extra="forbid"` throughout.
- `jinja2` — the launch prompt. Per the Code Guide, prompts are never f-strings.
- `structlog` — all logging. No `print` for observability; the CLI's own human-facing tables
  are `print`, which is output, not logging.
- `pytest` + `pre-commit` — as the template ships them.

**New dependency:** `pyyaml >= 6.0`.

- *What it does:* parses the five YAML config files the spec's interfaces are written in.
- *Alternatives ruled out:* `tomllib` (stdlib, but the spec's reviewed interfaces are YAML
  and re-spelling them as TOML would change a shape the reviewer already reacted to);
  `ruamel.yaml` (round-trip fidelity we do not need — nothing here rewrites a config).

**Stdlib, named because they replace things we would otherwise write:** `subprocess` (the
SSH seam), `smtplib` + `email.message` (email), **`urllib.request` (Slack incoming
webhooks — which is why Slack needs no SDK, no OAuth app and no dependency, and stops being
a deferred repo issue)**, `tempfile` + `os.replace` (the shims' atomic writes), `os.stat`
(the 0600 assertion on the credentials file), `uuid` (session ids), `json`, `shlex`,
`pathlib`, `re`.

**Notifications add zero dependencies.** Both channels are stdlib, which is the whole
reason email *and* Slack ship together here rather than Slack waiting for an issue.

**Not added, deliberately:** `paramiko` / `fabric` / `asyncssh`, `python-crontab`,
`click` / `typer`, `pandas`, `sqlalchemy`, `tenacity`, `asyncio`, `requests` / `httpx`,
`slack-sdk`. Each is justified in §0.

## 3. File locations

Grouped by file. Everything under `slurm_agent/` is a jupytext `py:percent` paired notebook
with `if test():` blocks beside each function, per the Code Guide.

**Repo root — new**

- `pyproject.toml` — already scaffolded on `main`; this design adds `[tool.poe] envfile = ".envrc"`
  (which is how every command gets the secrets, verified to accept `export KEY=v`, bare
  `KEY=v` and comments — so no `direnv` dependency), a `hc = {ref = "healthcheck"}` alias; the six runtime deps above; the
  `[project.scripts] slurm-agent = "slurm_agent.cli:app"` entry point; the `[tool.poe.tasks]`
  block (one `cmd` line per CLI command, plus the template's `nb`/`sync`/`clean`/`test`/
  `check`/`html`/`skill`, plus `hooks`); `[tool.juplit] notebook_src_dirs = ["slurm_agent",
  "docs"]` and `artifact_notebooks = ["sessions/**/*.py"]`;
  `[tool.pytest.ini_options] norecursedirs = ["slurm_agent/assets"]` so the shipped shim is
  never collected as a test module.
- `.gitignore` — on `main`; add `!sessions/**/*.ipynb` (the negation, or the session
  evidence is preserved locally and never committed), **`.envrc` (it holds the real
  secrets; the placeholder template is what is committed)**, `ledger.jsonl`, and
  `.slurm-agent/`
  (belt and braces — the run root lives on the cluster and outside every checkout, but a
  stray one inside a repo must never become a commit).
- `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `mkdocs.yml`, `docs/index.md` —
  on `main`, unmodified by this design.
- `slurm_agent/example.py` — **removed** in PR-01; it is the template's placeholder.
- `README.md` — on `main` as the template's; rewritten to setup plus the command inventory.
- `CLAUDE.md` — points at the Dev Workspace, names `.claude/skills/`, states the
  cluster-is-the-source-of-truth rule so a fresh session does not invent a local registry.
- `design.md` — this document.

**`config/` — new**

- `cluster.yaml`, `supervision.yaml`, `monitor.yaml` — three configs, with the spec's
  values as defaults. `monitor.yaml` now carries cadence and thresholds only.
- `manager.yaml` — the local session's own settings; `requires_env` names the environment
  **keys** it needs, mirroring `AgentConfig.requires_env`.
- `notify.yaml` — channels and recipients, shared by the digest, the supervision loop and
  the agents. **Contains no secrets and no path to any:** values live in the gitignored
  `.envrc` on each machine, and every committed YAML names keys only.
- `mcp.json` — the MCP server definitions `AgentConfig.mcp` names resolve against, passed
  to `claude --mcp-config`.

**`agents/` — new**

- `experiment-runner.yaml` — the spec's worked example, shipped as the reference agent.

**`slurm_agent/` — new** (one module per §1 heading)

- `__init__.py` — version, and `from juplit import test`.
- `config.py` — `ClusterConfig`, `ManagerConfig`, `AgentConfig`, `SupervisionConfig`,
  `MonitorConfig`, `load`, `duration_seconds`.
- `remote.py` — `Runner`, `RemoteError`, `ssh_runner`, `probe`.
- `jobs.py` — `Job`, `job_list`, `job_up`, `job_down`, `job_shell_command`,
  `update_node_config` (the last two ported from `slurm-ops`, which stays public and
  unchanged per the spec's Q&A).
- `staging.py` — `stage`, `missing_env`, `DirtyWorkdirError`.
- `launch.py` — `claude_argv`, `launch_prompt`, `prepare_run`, `launch`, `launch_batch`,
  `continue_run`, `MissingEnvError`, `LeaseExhausted`, `ContentionError`.
- `watch.py` — `AgentView`, `Decision`, `views`, `decide`, `act`, `watch`, `kill`,
  `HistoryRow`, `history`, `status_report`, `flush`.
- `monitor.py` — `usage`, `digest`, `monitor_run`, `cron_write`, `cron_status`.
- `notify.py` — `NotifyConfig`, `send_email`, `send_slack`, `notify`, `scrub`,
  `notify_test`, `NotifyError`.
- `preflight.py` — `Check`, `init`, `healthcheck`, `render`.
- `cli.py` — the cyclopts app; every command 1–3 lines.

**`slurm_agent/assets/` — new** (shipped data, never imported)

- `probe.sh` — the one-round-trip poll script, piped to `sh -s` on the login node.
- `remote_status.py` — the ~45-line atomic status writer copied to each run root at launch;
  verbs `tick` / `finish` (called by the hooks) and the agent's own `<state> --round …`.
- `hook_settings.json.jinja` — the `Stop` / `SessionEnd` hook settings rendered into each
  run root and passed as `claude --settings`.
- `remote_notify.py` — the ~40-line zero-dependency outbound shim copied to each run root;
  driven by the `SessionEnd` hook and callable by the agent on `needs_human`.

**`prompts/` — new**

- `agent_launch.md.jinja` — the launch prompt and the mailbox contract, including the
  instruction to announce completion on the task's PR.
- `job.sbatch.jinja` — the batch submission script; its last statement is the `claude` run,
  which is what makes a batch job end when its agent does.

**`ssh_config_templates/` — new**

- `config`, `tillicum-node-config` — copied from `slurm-ops` so `poe init --create` has
  something to install.

**`templates/` — new**

- `envrc.example` → copied by `poe init` to the repo-root `.envrc` (gitignored, chmod 0600),
  and the file a human copies to Tillicum by hand. One `KEY=<secret-here>` line per key
  declared across `manager.yaml` and every `agents/*.yaml`, each with a one-line comment.
  Every committed value is the literal `<secret-here>` — asserted by a test — and
  `poe healthcheck` fails while any declared key still reads it, so "copied" and "filled
  in" can never be confused.

**`sessions/` — new**

- `_template.py` — the artifact-notebook skeleton `poe session-new` copies, pre-shaped to
  the Experiment Guide's front-matter/round layout.

**`tests/` — new**

- `conftest.py` — `fake_runner(mapping)`, the dict-backed `Runner` the whole suite injects,
  plus `fake_smtp` / `fake_webhook` so no test ever sends a real message.
- `fixtures/` — `squeue.txt`, `probe.json`, `hyakusage.txt`, `envrc_sample`,
  `envrc_unfilled` (a copied-but-not-edited template, for the `<secret-here>` guard),
  `notebook_4cells.ipynb`.

**Removed:** `slurm_agent/example.py` (the template placeholder), in PR-01.

**Not touched:** `slurm-ops`, which stays public and unchanged per the spec's Q&A.

## 4. Testing outline

Every test is an `if test():` block beside its function unless it needs a shared fixture, in
which case it lives in `tests/`. Every error case in §1 has a bullet here.

**`config.py`**

- `load` — happy: a valid `agents/experiment-runner.yaml` round-trips to an `AgentConfig`.
- `load` — error: an unknown key raises `ValidationError` naming that key (`extra="forbid"`).
- `load` — error: a missing file raises `FileNotFoundError` mentioning `poe init`.
- `ManagerConfig` — happy: `requires_env` round-trips as a list of key names.
- **`templates/envrc.example` — the leak guard:** every value in the committed file is the
  literal `<secret-here>`. A real credential reaching this file is the one way a copyable
  template becomes a leak, so it is asserted rather than reviewed.
- **`templates/envrc.example` — the drift guard:** its key set equals the union of
  `manager.yaml`'s and every `agents/*.yaml`'s `requires_env`. Declare a key without adding
  it to the template and the test fails.
- `duration_seconds` — boundary: `"5m"`, `"04:00:00"`, `"90s"`, `"0"` all parse.
- `duration_seconds` — error: `"four hours"` raises `ValueError`.

**`remote.py`**

- `ssh_runner` — happy: builds the argv `["ssh", HOST, cmd]` (asserted without running ssh).
- `ssh_runner` — error: a non-zero exit raises `RemoteError` carrying stderr.
- `probe` — happy: `fixtures/probe.json` parses into the expected keys.
- `probe` — error: stdout with a login MOTD prepended raises `RemoteError` whose message
  contains the offending prefix.

**`jobs.py`**

- `job_list` — happy: `fixtures/squeue.txt` parses into two `Job`s with the right gpu counts.
- `job_list` — boundary: empty squeue output returns `[]`, not an error.
- `Job.gpu_usd` — happy: 4 GPUs x 2h at \$0.90 is \$7.20.
- `job_up` — happy: no existing job, the assembled command contains `salloc --no-shell` and
  the requested `--gpus`/`--time`/`--qos`.
- `job_up` — happy: an existing RUNNING job of that name is returned and `salloc` is never
  called (the reattach path).
- `job_up` — boundary: `allocation_mode="tmux"` wraps the same `salloc` in
  `tmux new-session -d`.
- `job_up` — boundary: still PENDING after `wait_s` returns the PENDING `Job`, no raise.
- `job_up` — boundary: **an existing interactive allocation under a different name is
  returned with a warning and no second `salloc` is issued** — the one-interactive-job cap.
- `job_down` — error: an unknown name raises `LookupError` listing the live job names.
- `update_node_config` — happy: rewrites `Hostname` in a tmp file, leaves other lines intact.

**`staging.py`**

- `stage` — happy: an absent workdir produces a `git clone` at the requested ref.
- `stage` — happy: an existing clean workdir fetches and checks out, returning the SHA.
- `stage` — error: `git status --porcelain` non-empty raises `DirtyWorkdirError` listing paths.
- `stage` — error: a workdir outside `$HOME`/scratch raises `ValueError`.
- `missing_env` — happy: `fixtures/envrc_sample` with `HF_TOKEN` set and `OPENAI_BASE_URL`
  absent returns exactly `["OPENAI_BASE_URL"]`.
- `missing_env` — boundary: an absent `.envrc` returns every required name.

**`launch.py`** — the highest-value block in the repo, because it is the audit surface

- `claude_argv` — happy: an `AgentConfig` produces argv containing `-p`, the given
  `--session-id`, `--permission-mode dontAsk`, `--max-budget-usd 8`, `--add-dir <workdir>`
  and every `allowed_tools` entry.
- `claude_argv` — boundary: **asserts `--bare` is never present** — it would disable the
  OAuth subscription auth the spec's Q&A says Tillicum uses, so this is a regression guard,
  not a style check.
- `claude_argv` — boundary: `mcp: []` omits both `--mcp-config` and `--strict-mcp-config`;
  a non-empty `mcp` emits both together.
- `claude_argv` — boundary: `resume=True` emits `--resume <session-id>` and keeps the flags.
- `claude_argv` — boundary: `--settings` points at the run root, and **no argv element and
  no rendered path is ever inside `agent.workdir`** — the regression guard for "nothing this
  repo writes lands inside a staged repo".
- `prepare_run` — happy: writes exactly five files, all under the run root; the fake runner
  sees no write whose path starts with `agent.workdir`.
- `prepare_run` — happy: the rendered `settings.json` parses, and its `Stop` and
  `SessionEnd` hook commands both name the absolute `remote_status.py` and the absolute
  notebook.
- `launch_batch` — happy: the rendered `job.sbatch` carries the requested `--time`/`--gpus`
  and its **last non-comment line is the `claude` invocation** (this is what makes the job
  self-terminating, so it is asserted, not assumed).
- `launch` — error: an allocation with no spare GPUs raises `ContentionError` naming the
  agents already on it; nothing is submitted.
- `remote_status.py tick` — happy: recomputes `cells_done` from a fixture `.ipynb` and
  refreshes `updated` **without** touching `round` / `waiting_on` (the hook must not erase
  what the agent said).
- `remote_status.py finish` — happy: writes the terminal state; boundary: `tick` on a run
  whose status file does not exist yet creates a valid one.
- `launch_prompt` — happy: the rendered prompt contains the task id, the notebook path and
  the `remote_status.py` invocation.
- `launch` — error: `missing_env` non-empty raises `MissingEnvError` and **nothing is
  submitted** (assert the fake runner saw no `srun`).
- `launch` — error: the named job is not RUNNING raises `LookupError` naming `poe job-up`.
- `continue_run` — happy: `leases_used` increments and the re-fired argv carries `--resume`.
- `continue_run` — error: `leases_used >= max_leases` raises `LeaseExhausted`.
- `assets/remote_status.py` — happy: writes valid JSON; boundary: a pre-existing block is
  replaced atomically and a `KeyboardInterrupt` mid-write leaves the old block readable.

**`watch.py`** — the policy, tested without a cluster or a clock

- `views` — happy: `fixtures/probe.json` merges into two `AgentView`s with both age fields.
- `views` — boundary: a run whose status block is missing yields `state="unknown"` with
  `status_age_s` taken from the file mtime.
- `decide` — one bullet per rule, each asserting **both the action and the `rule` string**:
  - `finished` → `escalate` ("wants review")
  - `failed` → `escalate`
  - `job_state="GONE"` while `running` → `escalate`
  - `needs_env` blocked past `blocked_for` → `kill`, rule `blocked_for`
  - `needs_env` blocked *under* `blocked_for` → `watch` (the boundary that stops a
    just-blocked run being killed on its first poll)
  - `status_age_s` past `status_stale_for` → `kill`, rule `status_stale_for`
  - `notebook_age_s` past `no_progress_for` → `kill`, rule `no_progress_for`
  - `gpu_idle_s` past `gpu_idle_for` → `kill`, rule `gpu_idle_for`
  - lease ending with leases left → `renew`; with none left → `escalate`
  - `finished` **and** announced → `done` (no message: the agent already spoke)
  - `finished` **and not** announced → `escalate` ("finished but never announced")
  - `mode="batch"` with `job_state="TIMEOUT"` and unfinished → `escalate`
  - `mode="batch"` near its walltime → `watch`, never `renew` (a batch deadline is real)
  - a healthy run → `watch`
  - **ordering:** a run that is both `finished` and stale returns `done`/`escalate`, never
    `kill` — a finished run must never be reported as killed for staleness.
- `act` — happy: a `kill` decision calls `scancel` once and returns a line naming the rule.
- `act` — boundary: killing one agent on a **shared** allocation cancels its step and
  leaves the allocation up while another agent is still on it (the neighbour-safety test).
- `act` — boundary: `renew` **without** `--auto-renew` submits nothing and returns a proposal.
- `act` — boundary: a repeated `escalate` for the same `(session_id, reason)` notifies once.
- `watch` — error: `probe` raising `RemoteError` skips the cycle and the loop survives
  (asserted with `once=False` and a runner that fails then succeeds).

**`watch.py` — history, status and flush**

- `history` — happy: a terminal run root joins its `sacct` row into one `HistoryRow` with
  both the job state and the agent's own state.
- `history` — boundary: a run root whose job `sacct` no longer knows is still listed, with
  `job_state="UNKNOWN"` — a forgotten job is not a forgotten run.
- `history` — boundary: an `sacct` row with no run root is skipped; flushing means
  forgetting, and history must not resurrect what flush removed.
- `status_report` — happy: four sections, each row in exactly one of them.
- `status_report` — **boundary, the disagreement case:** a run whose agent says `finished`
  but whose job says `FAILED` is listed under **failed**. The job is the harder fact, and
  reporting it as a success is the one wrong answer here.
- `flush` — happy: a completed run root older than the window is deleted and returned.
- `flush` — **error: a live session is never deleted**, whether its `status.json` is
  non-terminal or its job is still in `squeue` — asserted from both directions separately,
  because either check alone would let one class of live run through.
- `flush` — boundary: without `--failed`, failed roots survive; with it, they go.
- `flush` — boundary: `--dry-run` returns the list and the fake runner sees no `rm`.
- `flush` — **boundary: no path outside `cluster.run_root` is ever passed to `rm`** — the
  guard against a malformed session id reaching a delete.

**`monitor.py`**

- `usage` — happy: `fixtures/hyakusage.txt` parses to the expected account totals.
- `usage` — error: unparseable output raises `ValueError` containing the raw first lines.
- `digest` — happy: spend moved since the last sent row produces a body naming the delta.
- `digest` — boundary: unchanged spend with `only_if_changed` returns `None`.
- `digest` — boundary: compares against the last **sent** row, not the last observed one —
  three unsent polls then a change still produces a digest.
- `digest` — boundary: crossing `budget_used_pct` produces a body even when spend is flat.
- `digest` — boundary: batch jobs that finished since the last digest appear in the body
  with their exit states, and a digest with only batch news is still sent.
- `cron_write` — happy: installing twice leaves exactly one marked block.
- `cron_write` — happy: removing restores the crontab byte-for-byte, other entries intact.
- `cron_write` — error: no `crontab` binary raises `RuntimeError` naming launchd/systemd.

**`notify.py`**

- **Scrubbing — the security-relevant one:** `scrub` replaces every declared key's value
  wherever it appears, and an SMTP rejection echoing the password produces a `NotifyError`
  whose string contains **none** of them. Asserted by seeding a distinctive password.
- `send_email` — happy: against a fake SMTP, issues `starttls` → `login` → `send_message`
  with the subject and recipient given.
- `send_slack` — happy: POSTs `{"text": …}` to the webhook URL from the secrets.
- `send_slack` — error: a non-2xx response raises `NotifyError` carrying the status.
- `notify` — boundary: one channel raising still returns the other as delivered, and does
  not raise. A reporter of crashes must not crash.
- `notify` — error: every channel failing raises `NotifyError` (and `watch.act` logs it and
  keeps the loop alive — asserted there).
- `remote_notify.py --from-status` — happy: builds subject and body from a fixture status
  block and writes the delivered channels back into it.
- `remote_notify.py` — boundary: with no credentials file it exits non-zero **without
  touching the status block**, so a notify failure cannot corrupt supervision state.

**`preflight.py`**

- `healthcheck` — happy: everything wired produces `ok=True` rows against a fake runner.
- `healthcheck` — boundary: a failing check yields `ok=False` with a non-empty `fix`.
- `healthcheck` — boundary: **the fast tier runs no expensive probe** — the fake runner
  sees no `salloc`, no `claude`, no `sbatch`, and the fake SMTP/webhook record zero calls.
  This is what makes `poe hc` worth typing after every network change, so it is asserted.
- `healthcheck` — boundary: `full=True` adds exactly the three slow rows.
- `healthcheck` — boundary: `send=True` really sends, once per channel **per machine** —
  the fakes record one local and one cluster-side call each.
- `healthcheck` — boundary: `send=False` leaves both send rows at `ok=None`, and `render`
  shows SKIPPED. A skipped proof must never render as a passed one.
- `healthcheck` — error: a `.envrc` at 0644 yields `ok=False`, not a warning.
- `healthcheck` — error: a declared key that is unset, and one still reading
  `<secret-here>`, both fail — and **the report names the keys but no values** (asserted by
  seeding a distinctive value and searching the rendered text).
- `healthcheck` — boundary: ssh unreachable fails the identity row and marks every
  cluster-side row SKIPPED, so one broken link does not render as eight problems.
- `init` — happy: on a fresh clone it creates `.envrc` from the template at mode 0600, with
  every declared key present.
- `init` — boundary: `.envrc` already present is **never overwritten** — the one file in
  this design that holds irreplaceable human input.
- `init` — boundary: the `.envrc` it just created still **fails** the healthcheck rows,
  because `<secret-here>` is not a credential. Creating and satisfying are different jobs.
- `render` — happy: renders `name / ok|MISSING|SKIPPED / detail`.

**`cli.py`**

- Smoke: `slurm-agent --help` lists every command in the §1 inventory (guards against a
  command being added to the CLI but not to `poe`, and vice versa — the test reads both the
  cyclopts app and `pyproject.toml` and asserts the two sets are equal).

**Not tested here:** anything requiring a real Tillicum connection. Those are the
`poe init` probes in Appendix A, run once against the cluster during PR-02 and PR-04, with
their captured output committed as the fixtures the tests above read.

## 5. Estimated scope

Roughly **36 files, ~2,270 lines added, 0 modified** — a greenfield repo, so nearly all of
it is new; ~40% of the Python is `if test():` blocks and their fixtures. The review's batch
mode and hooks added ~200 lines net, which is small because both reuse the interactive
path wholesale: batch changes only how the job is submitted, and the hooks are JSON.

- `pyproject.toml`, `.gitignore`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml` —
  ~150 added (mostly the poe inventory)
- `slurm_agent/config.py` — ~130
- `slurm_agent/remote.py` — ~90
- `slurm_agent/assets/probe.sh` — ~35
- `slurm_agent/assets/remote_status.py` — ~45
- `slurm_agent/assets/hook_settings.json.jinja` — ~20
- `prompts/job.sbatch.jinja` — ~30
- `slurm_agent/jobs.py` — ~190
- `slurm_agent/staging.py` — ~110
- `slurm_agent/launch.py` — ~300 (`prepare_run` + `launch` + `launch_batch`)
- `slurm_agent/watch.py` — ~390 (supervision, plus history / status / flush)
- `slurm_agent/monitor.py` — ~170
- `slurm_agent/notify.py` — ~120 (two channels, `scrub`, `notify_test`; no creds reader)
- `slurm_agent/assets/remote_notify.py` — ~40
- `config/notify.yaml` — ~15
- `templates/envrc.example` — ~30 (nearly all comments)
- `config/manager.yaml` — ~10
- `slurm_agent/preflight.py` — ~200 (`init` + a two-tier `healthcheck`)
- `slurm_agent/cli.py` — ~120
- `config/*.yaml`, `config/mcp.json`, `agents/experiment-runner.yaml` — ~90
- `prompts/agent_launch.md.jinja`, `sessions/_template.py`, `ssh_config_templates/*` — ~90
- `README.md`, `CLAUDE.md`, `.claude/skills/slurm-orchestration.md` — ~200
- `tests/conftest.py` + fixtures — ~90

**The smallest diff that satisfies the spec.** Two things could be deleted without failing a
test and are the first candidates if the reviewer wants this smaller:

- `sessions/_template.py` and `poe session-new` (~25 lines). A human can copy a file. Kept
  only so the Experiment Guide's shape is the default rather than something each session
  re-derives.
- `agent-logs --cells` (~15 lines). `poe job-shell` plus `juplit cells` by hand does the
  same thing; the command exists because the spec named it and because doing it remotely
  is what keeps a 4 MB notebook off the laptop.

If the scope still reads large: it is one repo covering four capabilities the spec asked
for (allocations, remote agents, supervision, monitoring). The §6 split is what makes that
reviewable — the first three PRs are useful on their own even if the rest never lands.

## 6. Stacking plan

Ten PRs, bottom first. Every layer builds and passes CI with nothing above it merged, and
every layer ships its own tests. Branches are rooted at
`claude/slurm-agent-orchestration-v8jauv`.

- **`claude/slurm-agent-orchestration-v8jauv-01-scaffold`**
  - *Lands:* `config.py` and the four pydantic models with their YAML files, plus
    `ManagerConfig` and `config/manager.yaml`; `templates/envrc.example` and the
    `[tool.poe] envfile` wiring; `remote.py` (`ssh_runner`, `RemoteError`); the cyclopts app
    and the poe inventory as stubs; `tests/conftest.py`, `CLAUDE.md`. Deletes the
    template's `slurm_agent/example.py`.
  - *Already on `main`:* the cookiecutter scaffold — poe tasks, jupytext pairing, the juplit
    pre-commit hooks and CI — committed as the repo's root commit, so no PR spends review
    on generated boilerplate.
  - *Stands alone because:* `poe test` and `poe check` are green and `slurm-agent --help`
    prints the inventory. A reviewer can read the whole audit surface — every config shape
    and every command name — before a single cluster call exists.
  - *Depends on:* nothing — branches off the task branch.
- **`…-02-jobs`**
  - *Lands:* `jobs.py` in full, plus `job-up` / `job-status` / `job-shell` / `job-down`.
  - *Stands alone because:* it is immediately useful to a human on its own — this is
    `slurm-ops` that actually runs the commands instead of printing them, and it is where the
    `salloc --no-shell` assumption gets verified against the real cluster.
  - *Depends on:* `…-01-scaffold`.
- **`…-03-staging`**
  - *Lands:* `staging.py` (`stage`, `missing_env`) and the `ssh_config_templates/`.
  - *Stands alone because:* staging a repo at a ref and reporting missing env vars is a
    complete, separately useful capability; no agent needs to exist for it to be worth having.
  - *Depends on:* `…-02-jobs`.
- **`…-04-launch`**
  - *Lands:* `launch.py` (`claude_argv`, `prepare_run`, `launch`), `prompts/agent_launch.md.jinja`,
    `assets/remote_status.py`, `assets/hook_settings.json.jinja`,
    `agents/experiment-runner.yaml`, `config/mcp.json`; `agent-run` / `agent-logs`.
  - *Stands alone because:* it launches and inspects one supervised-by-hand agent end to
    end, hooks included. Supervision is a separate argument and should be reviewed as one.
  - *Depends on:* `…-03-staging`.
- **`…-05-notify`**
  - *Lands:* `notify.py`, `assets/remote_notify.py`, `config/notify.yaml`,
    the `SessionEnd` notify hook, and `poe notify-test`.
  - *Stands alone because:* "I can reach you on email and Slack, from the laptop and from
    Tillicum" is a complete capability with its own proof — `poe notify-test` — and it
    needs nothing above it. It sits here rather than with the monitor because it has three
    consumers above it (supervision escalations, the digest, and the agents), so shipping
    it once below all three is what keeps it from being written twice.
  - *Depends on:* `…-04-launch` (the run root and the hook settings it extends).
- **`…-06-supervise`**
  - *Lands:* `assets/probe.sh` and `remote.probe`; `watch.py` entire; `agent-status` /
    `agent-watch` / `agent-kill` / `agent-continue`.
  - *Stands alone because:* it is the whole policy — the pure `decide` plus its threshold
    tests — and it is the layer most worth arguing about line by line.
  - *Depends on:* `…-05-notify` (escalation sends).
- **`…-07-status`**
  - *Lands:* the `sacct` extension to `probe.sh`; `HistoryRow`, `history`, `status_report`,
    `flush`; `poe status` and `poe flush`.
  - *Stands alone because:* "tell me what is running, queued, done and failed — and let me
    tidy the list" is a complete capability a human uses on its own, and it is the only
    layer that **deletes** anything, which is worth isolating for review rather than
    burying inside supervision.
  - *Depends on:* `…-06-supervise` (it reuses `views` and the probe).
- **`…-08-batch`**
  - *Lands:* `launch_batch`, `prompts/job.sbatch.jinja`, `AgentConfig.mode`, `agent-batch`;
    the `TIMEOUT` and never-renew-a-batch-job branches of `decide`.
  - *Stands alone because:* it is a second submission path over machinery that already
    exists and is already tested, and it is the only way to run anything while the single
    interactive allocation is in use. Reviewing it separately keeps the interactive
    argument in PR-04/05 from being re-opened by sbatch details.
  - *Depends on:* `…-07-status` (it adds the `TIMEOUT` rows both `decide` and `status` read).
- **`…-09-monitor`**
  - *Lands:* `monitor.py`, `config/monitor.yaml`; the four `monitor-*` commands.
  - *Stands alone because:* the usage digest shares only `remote.run` and `notify` with
    everything above it. It is late because it is the least urgent, not the most coupled.
  - *Depends on:* `…-05-notify` in principle, `…-08-batch` in practice (stacked to keep the
    merge order linear rather than because it needs the code).
- **`…-10-init-skills-docs`**
  - *Lands:* `preflight.py`, `poe init`, and `poe healthcheck` / `poe hc` with both tiers;
    `.claude/skills/slurm-orchestration.md`; `sessions/_template.py` and `poe session-new`;
    `docs/setup.md` (where a human is told how to fill in `.envrc` on both machines); the
    README command reference.
  - *Stands alone because:* the healthcheck can only check things once they all exist, and the
    skill can only describe a workflow once the workflow runs. This layer is the repo
    becoming self-describing.
  - *Depends on:* `…-09-monitor`.

A plan, not a contract: an Execute session that finds a better cut may take it and say so in
the PR body. Folding **09** into **10**, or **03** into **04**, are the two most likely.

## Appendix A — assumptions to verify on first contact

Neither this design session nor any Claude session can reach Tillicum (that is the spec's
whole premise), so the things below are reasoned, not observed. Each has a named owner PR
and a named fallback, and `poe init` is where they become checks rather than assumptions.

- **`salloc --no-shell` is permitted, and the allocation outlives the SSH connection.**
  - *Owner:* PR-02, and the `ALLOCATION PROBE` check in `poe init`.
  - *Fallback:* `allocation_mode: tmux` — a detached `tmux` session on the login node holding
    an ordinary `salloc`, which is what `slurm-ops` does today. Both paths are in `job_up`
    from the start, so the fallback is a config value, not a rewrite.
  - *Why it matters:* every lease depends on it (spec Q&A).
- **A detached `srun` step survives the SSH connection that started it.**
  - *Owner:* PR-04.
  - *Fallback:* start the step inside the same login-node `tmux` session that holds the
    allocation. Note the residual limitation either way: the `srun` client runs on the login
    node, so a login-node reboot ends the step. Bounded leases are what make that survivable.
- **`claude -p` authenticates non-interactively on Tillicum under the user's subscription,
  and still reports a cost.**
  - *Owner:* PR-04, and the `AGENT CREDENTIAL` check in `poe init` — deliberately a
    one-token round trip on the *login* node, so it costs nothing and runs before any GPU.
    The check asserts two things: that the run succeeds, and that `total_cost_usd` comes
    back **greater than zero**, because a subscription that reports zero would silently
    disarm `--max-budget-usd`.
  - *Partly verified already:* run against the CLI while writing this design,
    `claude -p --output-format json --max-budget-usd 5` returned `total_cost_usd` with a
    per-model breakdown carrying `"costBasis": "list"` — so the CLI prices runs at API list
    rates. What remains unverified is only that this holds under *subscription* auth on
    Tillicum specifically, which is what the `poe init` assertion pins down.
  - *Fallback if cost comes back zero:* `--max-turns` as the runaway guard instead. Note it
    exits non-zero, so `launch` must translate that exit into a `state="failed"` result
    rather than treating it as a crash.
  - *Note:* this is why `claude_argv` never emits `--bare`, and why there is a test
    asserting so.
- **Tillicum permits exactly one interactive allocation per user.**
  - *Owner:* PR-02. Taken from the review as a statement of fact about the cluster; the
    `ALLOCATION PROBE` check confirms it by observing what a second `salloc` does.
  - *Fallback:* if the cap turns out to be higher or absent, `job_up`'s guard becomes a
    warning instead of a refusal — a one-line change. Nothing else in the design depends on
    the cap being exactly one, because agents share an allocation as `--overlap` steps
    either way.
- **Hooks fire for a `claude -p` run started under `srun` / `sbatch`.**
  - *Owner:* PR-04. `Stop` and `SessionEnd` are confirmed hook events in the installed CLI,
    and `--settings` is the documented delivery path; what is unobserved is whether a
    non-interactive, non-TTY run on a compute node fires them as expected.
  - *Fallback:* liveness falls back to the observed signal alone — `no_progress_for` on the
    notebook mtime, which needs no cooperation from the agent or the CLI. `status_stale_for`
    would then revert to depending on the agent's own calls, which is the weaker position
    this design moved away from, so it is worth confirming early.
- **`sbatch` is available to this account with a partition that accepts these walltimes.**
  - *Owner:* PR-07.
  - *Fallback:* none needed for correctness — without batch, the design is the interactive
    half only, which is what the spec originally asked for.
- **Tillicum's compute nodes can reach an SMTP server and `hooks.slack.com` outbound.**
  - *Owner:* PR-05, and the **`NOTIFY SEND (cluster)`** row in `poe init`, which really
    sends from Tillicum over ssh — so this assumption is settled by the setup command on
    first run, in front of the person who can fix it, rather than at 3am by its absence.
    The spec's Q&A establishes general outbound access from compute nodes, but that was
    asked about the Anthropic API and MCP servers, not about port 587 — a site that allows
    HTTPS egress and blocks SMTP is an ordinary configuration.
  - *Fallback, in order:* Slack only (plain HTTPS, so it survives an SMTP block); then the
    agent writing its message into the status block for the laptop to forward on the next
    poll — which loses the 3am delivery this feature exists for, so it is a real
    degradation and worth knowing about on day one rather than the first night.
- **`hyakusage` exists and its output is stable enough to parse.**
  - *Owner:* PR-06. The first act is to capture a real sample into
    `tests/fixtures/hyakusage.txt`; the parser is written against that file.
  - *Fallback:* `sacct`-derived GPU-hours. Less accurate about budget, sufficient for the
    change-gate, and reachable with the same `remote.run`.
- **`nvidia-smi` is reachable through `srun --jobid --overlap` on a running allocation.**
  - *Owner:* PR-05.
  - *Fallback:* drop `gpu_idle_for` from the shipped `supervision.yaml` defaults. It is the
    weakest of the four thresholds — `no_progress_for` catches nearly everything it would —
    so losing it costs little.

## Appendix B — what this design deliberately does not decide

- **Klone, and log archival to moana.** Deferred to repo issues opened once this ships
  (spec, *Out of scope*). `ClusterConfig` is why Klone is not designed *out*; nothing here
  claims it is designed *in*.
- ~~**Slack notifications.**~~ **Now in scope.** The spec's Q&A said email first and Slack
  as a repo issue; an incoming webhook turns out to be a `urllib.request` POST of
  `{"text": …}`, so the "research what we need for a Slack version" the Q&A asked for has
  an answer — nothing, no dependency and about five lines — and deferring it would cost
  more in coordination than writing it. Like batch mode, this **supersedes a spec Q&A
  answer** and the spec should be amended rather than left disagreeing.
- ~~**Batch jobs for genuinely long work.**~~ **Now in scope**, at the review's request and
  because the one-interactive-allocation cap makes it necessary rather than optional. See
  *Two execution modes*. This **supersedes the spec's Q&A note** that batch was a
  post-ship feature request; the spec should be amended rather than left contradicting the
  design.
- **What a remote agent should conclude.** Run layout, metrics and analysis stay owned by
  the repo running the experiment (spec, *Out of scope*). This repo gets the compute,
  launches the agent, and stops.

## Appendix C — two rulings, both now made

Both touched an approved-spec bullet, so both went back to the author. Recorded here so the
reasoning survives the PR thread.

- **Should the *scheduled* monitor be able to submit batch jobs? — NO. Manager only.**
  - *Ruled:* 2026-08-27. The design keeps the conservative branch: `agent-batch` belongs to
    the **manager** (the supervising session, which already holds kill authority), and the
    **scheduled** usage monitor gains only a *report* on how batch jobs went. It still
    never acts on the cluster, so the spec's out-of-scope bullet stands unamended.
  - *The argument that settled it:* the scheduled monitor is a **crontab entry on the
    laptop**, and a sleeping laptop runs no cron (macOS does not replay missed entries). So
    giving it submission authority would not have bought overnight autonomy at all — batch
    mode alone already does that, because SLURM runs the job once `sbatch` returns and
    nothing local needs to be awake.
  - *What it would have cost:* submit and kill authority coming apart. Every cost guard —
    `blocked_for`, `status_stale_for`, `no_progress_for`, `gpu_idle_for` — lives inside
    `agent-watch`. An unattended submitter creates jobs with no kill switch: bounded by
    walltime (a 4-GPU 12h job is ~\$43), but recurring every three days.
  - *Left on the table, deliberately:* having the digest **propose** a command
    ("gpu-h200 idle 14h · TASK-104 pending · run: `poe agent-batch …`") — ~15 lines, no
    unattended authority. Worth revisiting once there are real digests to judge, not now.
- **May a secret live on Tillicum so agents can email/Slack you directly? — YES.**
  - *Ruled:* 2026-08-27. This **reverses the spec's** "This repo never writes secrets to the
    shared filesystem", and it is in this design rather than a follow-up.
  - *What changed:* agents send email and Slack from the compute node, via the `SessionEnd`
    hook and `assets/remote_notify.py`. Slack ships alongside email rather than as the
    deferred repo issue the spec's Q&A imagined, because an incoming webhook is a stdlib
    POST — see Appendix B.
  - *What did not change — and this is the part worth holding to:* the spec's sentence is
    reversed only as to **storage**, not handling. Secrets live in the gitignored `.envrc`
    on each machine — **the mechanism the spec already uses for every experiment repo**,
    not a new one — and the committed YAML names keys only, never values, exactly as
    `AgentConfig.requires_env` already did. `poe init` writes the placeholder template and
    never a value; `poe healthcheck` fails a `.envrc` the group or world can read, which on
    a shared filesystem is the real exposure; and every message leaving `notify.py` is run
    through `scrub()` so a bad password echoed by an SMTP server cannot reach a log, a
    status block or a committed notebook.
  - *And it is verified rather than assumed:* `poe init` runs a full healthcheck that
    really sends from **both** machines, so a clone is not considered set up until two real
    messages have arrived.

## Approval

Reviewer: leave line comments on anything above. On approval I will check `Design Approved`
on the task, switch to **Code Implementation**, load **Semantic PRs**, and start at
`…-01-scaffold`.
