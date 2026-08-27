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

- **`poe init` absorbs the template's `poe init`, which becomes `poe hooks`.**
  - *Was:* juplit_template ships `init = pre-commit install`; the spec wants `poe init` to
    be the fresh-clone footprint check.
  - *Now:* `poe init` is the spec's check, and installing the git hooks is its first step.
    `poe hooks` remains for re-installing hooks alone.
  - *Why:* two commands cannot share a name, and the spec's meaning is the one a human
    reaching for `init` on a fresh clone expects.
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

### Who tells the human, and when

The review asked for agents to announce their own completion so the manager only speaks
about failures. That is the right split, and most of it lands cleanly:

- **The agent announces success itself, through GitHub.** It already has a git credential
  on Tillicum (spec Q&A) and the GitHub MCP, and the spec already says a finished run
  points the human at its experiment notebook *in its PR*. So "done, wants review" is a PR
  comment the agent writes — no new credential, no new channel.
- **The local side speaks only about failures and silences:** crashed, `TIMEOUT`, killed by
  a threshold, or `finished` with no PR comment (the agent could not announce itself). That
  is the review's "only tell me if they crashed or could not send the message", and it
  falls out of `decide` with one extra rule rather than new machinery.
- **Email from the compute node is *not* designed here.** It would need an SMTP credential
  or webhook on the shared filesystem, and the spec is explicit that this repo never writes
  secrets to Tillicum. Flagged for a ruling rather than decided — see Appendix B.

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
  notification channel. Slack is a repo issue (spec Q&A), and it will be a second function.

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
- **`monitor` — the change-gated usage digest**
  - *Needs to exist:* yes, the spec's own motivating story ("\$86 nobody noticed").
  - *Already solved:* `smtplib`/`email.message` for sending, `crontab -l | ... | crontab -`
    for the schedule (marker-delimited block, idempotent both ways — no `python-crontab`
    dependency for 25 lines of text munging).
  - *Smallest form:* `usage()` (parse `hyakusage`), `digest()` (compare against the last
    **sent** ledger row), and three cron functions.
- **`preflight` — `poe init`**
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
    model_config = ConfigDict(extra="forbid")
    every_days: int = 3
    only_if_changed: bool = True
    channel: Literal["email"] = "email"  # slack is a repo issue (spec Q&A)
    to: str
    budget_used_pct: int = 80
    idle_gpu_hours: float = 2.0


def load(path: Path, model: type[T]) -> T:
    """Parse one YAML file into one pydantic model. The only config entry point."""
    # yaml.safe_load(path.read_text())
    # model.model_validate(data)  -- extra="forbid" turns a typo into a load-time error
    raise NotImplementedError


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
    {"now":…, "jobs":[…squeue rows…], "runs":[{launch…, "status":{…}, "nb_mtime":…,
    "nb_bytes":…, "gpu_util":…}]}. Everything the supervision loop needs, one read,
    independent of how many agents are in flight.
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
    #     run_dir/settings.json      <- render assets/hook_settings.json.jinja:
    #           Stop       -> python RUN_DIR/remote_status.py tick --notebook <abs .ipynb>
    #           SessionEnd -> python RUN_DIR/remote_status.py finish --notebook <abs .ipynb>
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

```python
def send(subject: str, body: str, *, to: str) -> None:
    """One email, via smtplib + email.message. The only outbound channel today."""
    # Slack is a repo issue per the spec Q&A; it will be a sibling function, not a Notifier ABC
    raise NotImplementedError
```

### `slurm_agent/preflight.py` — `poe init`

```python
class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    ok: bool
    detail: str
    fix: str | None = None


def check_all(cluster: ClusterConfig, *, create: bool = True) -> list[Check]:
    """Everything a fresh clone needs, checked and (where safe) created.

    Two of these exist because the spec's Q&A demanded early verification of assumptions
    every lease depends on. Finding out here costs a second; finding out at 3am costs a run.
    """
    # git hooks         : pre-commit installed        (create: `pre-commit install`)
    # ssh config        : login + node hosts present  (create: from ssh_config_templates/)
    # cluster identity  : `ssh HOST id -un` works, ControlMaster socket live
    # run root          : cluster.run_root exists     (create: mkdir -p over ssh)
    # ALLOCATION PROBE  : `salloc --no-shell --time=00:01:00 …` then scancel — does the
    #                     allocation outlive the ssh that asked for it? Sets/echoes
    #                     allocation_mode, and reports whether a SECOND interactive
    #                     allocation is refused. VERIFIES the biggest lease assumption.
    # AGENT CREDENTIAL  : `ssh HOST 'claude -p "reply OK" --output-format json'` — proves the
    #                     subscription auth on Tillicum works headlessly, before any GPU
    #                     spend, AND asserts total_cost_usd > 0: a subscription that reports
    #                     zero cost would silently disarm --max-budget-usd
    # BATCH             : `sbatch --test-only` on the rendered template — the partition
    #                     accepts our walltime, checked without queueing anything
    # notifications     : monitor.yaml present and `to:` set
    # usage monitor     : cron block installed        (report only — never install silently)
    raise NotImplementedError


def render(checks: list[Check]) -> str:
    """The spec's two-column report: name, ok/MISSING, detail. Exit non-zero if any failed."""
    raise NotImplementedError
```

### `slurm_agent/cli.py`

```python
app = cyclopts.App(name="slurm-agent")

@app.command
def init(create: bool = True) -> None: ...
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
SSH seam), `smtplib` + `email.message` (the digest), `tempfile` + `os.replace` (the shim's
atomic write), `uuid` (session ids), `json`, `shlex`, `pathlib`, `re`.

**Not added, deliberately:** `paramiko` / `fabric` / `asyncssh`, `python-crontab`,
`click` / `typer`, `pandas`, `sqlalchemy`, `tenacity`, `asyncio`. Each is justified in §0.

## 3. File locations

Grouped by file. Everything under `slurm_agent/` is a jupytext `py:percent` paired notebook
with `if test():` blocks beside each function, per the Code Guide.

**Repo root — new**

- `pyproject.toml` — already scaffolded on `main`; this design adds: the six runtime deps above; the
  `[project.scripts] slurm-agent = "slurm_agent.cli:app"` entry point; the `[tool.poe.tasks]`
  block (one `cmd` line per CLI command, plus the template's `nb`/`sync`/`clean`/`test`/
  `check`/`html`/`skill`, plus `hooks`); `[tool.juplit] notebook_src_dirs = ["slurm_agent",
  "docs"]` and `artifact_notebooks = ["sessions/**/*.py"]`;
  `[tool.pytest.ini_options] norecursedirs = ["slurm_agent/assets"]` so the shipped shim is
  never collected as a test module.
- `.gitignore` — on `main`; add `!sessions/**/*.ipynb` (the negation, or the session
  evidence is preserved locally and never committed), `ledger.jsonl`, and `.slurm-agent/`
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

- `cluster.yaml`, `supervision.yaml`, `monitor.yaml` — the three configs, with the spec's
  values as defaults.
- `mcp.json` — the MCP server definitions `AgentConfig.mcp` names resolve against, passed
  to `claude --mcp-config`.

**`agents/` — new**

- `experiment-runner.yaml` — the spec's worked example, shipped as the reference agent.

**`slurm_agent/` — new** (one module per §1 heading)

- `__init__.py` — version, and `from juplit import test`.
- `config.py` — `ClusterConfig`, `AgentConfig`, `SupervisionConfig`, `MonitorConfig`, `load`,
  `duration_seconds`.
- `remote.py` — `Runner`, `RemoteError`, `ssh_runner`, `probe`.
- `jobs.py` — `Job`, `job_list`, `job_up`, `job_down`, `job_shell_command`,
  `update_node_config` (the last two ported from `slurm-ops`, which stays public and
  unchanged per the spec's Q&A).
- `staging.py` — `stage`, `missing_env`, `DirtyWorkdirError`.
- `launch.py` — `claude_argv`, `launch_prompt`, `prepare_run`, `launch`, `launch_batch`,
  `continue_run`, `MissingEnvError`, `LeaseExhausted`, `ContentionError`.
- `watch.py` — `AgentView`, `Decision`, `views`, `decide`, `act`, `watch`, `kill`.
- `monitor.py` — `usage`, `digest`, `monitor_run`, `cron_write`, `cron_status`.
- `notify.py` — `send`.
- `preflight.py` — `Check`, `check_all`, `render`.
- `cli.py` — the cyclopts app; every command 1–3 lines.

**`slurm_agent/assets/` — new** (shipped data, never imported)

- `probe.sh` — the one-round-trip poll script, piped to `sh -s` on the login node.
- `remote_status.py` — the ~45-line atomic status writer copied to each run root at launch;
  verbs `tick` / `finish` (called by the hooks) and the agent's own `<state> --round …`.
- `hook_settings.json.jinja` — the `Stop` / `SessionEnd` hook settings rendered into each
  run root and passed as `claude --settings`.

**`prompts/` — new**

- `agent_launch.md.jinja` — the launch prompt and the mailbox contract, including the
  instruction to announce completion on the task's PR.
- `job.sbatch.jinja` — the batch submission script; its last statement is the `claude` run,
  which is what makes a batch job end when its agent does.

**`ssh_config_templates/` — new**

- `config`, `tillicum-node-config` — copied from `slurm-ops` so `poe init --create` has
  something to install.

**`sessions/` — new**

- `_template.py` — the artifact-notebook skeleton `poe session-new` copies, pre-shaped to
  the Experiment Guide's front-matter/round layout.

**`tests/` — new**

- `conftest.py` — `fake_runner(mapping)`, the dict-backed `Runner` the whole suite injects.
- `fixtures/` — `squeue.txt`, `probe.json`, `hyakusage.txt`, `envrc_sample`.

**Removed:** `slurm_agent/example.py` (the template placeholder), in PR-01.

**Not touched:** `slurm-ops`, which stays public and unchanged per the spec's Q&A.

## 4. Testing outline

Every test is an `if test():` block beside its function unless it needs a shared fixture, in
which case it lives in `tests/`. Every error case in §1 has a bullet here.

**`config.py`**

- `load` — happy: a valid `agents/experiment-runner.yaml` round-trips to an `AgentConfig`.
- `load` — error: an unknown key raises `ValidationError` naming that key (`extra="forbid"`).
- `load` — error: a missing file raises `FileNotFoundError` mentioning `poe init`.
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

**`preflight.py`**

- `check_all` — happy: all checks pass against a fake runner and produce `ok=True` rows.
- `check_all` — boundary: a failing check yields `ok=False` with a non-empty `fix`.
- `check_all` — boundary: `create=False` never mutates (the fake runner sees no `mkdir`).
- `render` — happy: renders the spec's `name / ok|MISSING / detail` columns.

**`cli.py`**

- Smoke: `slurm-agent --help` lists every command in the §1 inventory (guards against a
  command being added to the CLI but not to `poe`, and vice versa — the test reads both the
  cyclopts app and `pyproject.toml` and asserts the two sets are equal).

**Not tested here:** anything requiring a real Tillicum connection. Those are the
`poe init` probes in Appendix A, run once against the cluster during PR-02 and PR-04, with
their captured output committed as the fixtures the tests above read.

## 5. Estimated scope

Roughly **31 files, ~1,850 lines added, 0 modified** — a greenfield repo, so nearly all of
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
- `slurm_agent/watch.py` — ~290
- `slurm_agent/monitor.py` — ~170
- `slurm_agent/notify.py` — ~30
- `slurm_agent/preflight.py` — ~130
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

Eight PRs, bottom first. Every layer builds and passes CI with nothing above it merged, and
every layer ships its own tests. Branches are rooted at
`claude/slurm-agent-orchestration-v8jauv`.

- **`claude/slurm-agent-orchestration-v8jauv-01-scaffold`**
  - *Lands:* `config.py` and the four pydantic models with their YAML files; `remote.py`
    (`ssh_runner`, `RemoteError`); the cyclopts app and the poe inventory as stubs;
    `tests/conftest.py`, `CLAUDE.md`. Deletes the template's `slurm_agent/example.py`.
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
- **`…-05-supervise`**
  - *Lands:* `assets/probe.sh` and `remote.probe`; `watch.py` entire; `agent-status` /
    `agent-watch` / `agent-kill` / `agent-continue`.
  - *Stands alone because:* it is the whole policy — the pure `decide` plus its threshold
    tests — and it is the layer most worth arguing about line by line.
  - *Depends on:* `…-04-launch`.
- **`…-06-batch`**
  - *Lands:* `launch_batch`, `prompts/job.sbatch.jinja`, `AgentConfig.mode`, `agent-batch`;
    the `TIMEOUT` and never-renew-a-batch-job branches of `decide`.
  - *Stands alone because:* it is a second submission path over machinery that already
    exists and is already tested, and it is the only way to run anything while the single
    interactive allocation is in use. Reviewing it separately keeps the interactive
    argument in PR-04/05 from being re-opened by sbatch details.
  - *Depends on:* `…-05-supervise` (it extends `decide`).
- **`…-07-monitor`**
  - *Lands:* `monitor.py`, `notify.py`, `config/monitor.yaml`; the four `monitor-*` commands.
  - *Stands alone because:* the usage digest shares only `remote.run` with everything above
    it. It could genuinely have shipped first; it is last because it is the least urgent.
  - *Depends on:* `…-01-scaffold` in principle, `…-06-batch` in practice (stacked to keep
    the merge order linear rather than because it needs the code).
- **`…-08-init-skills-docs`**
  - *Lands:* `preflight.py` and `poe init`; `.claude/skills/slurm-orchestration.md`;
    `sessions/_template.py` and `poe session-new`; the README command reference.
  - *Stands alone because:* `poe init` can only check things once they all exist, and the
    skill can only describe a workflow once the workflow runs. This layer is the repo
    becoming self-describing.
  - *Depends on:* `…-07-monitor`.

A plan, not a contract: an Execute session that finds a better cut may take it and say so in
the PR body. Folding **07** into **08**, or **03** into **04**, are the two most likely.

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
  - *Owner:* PR-06.
  - *Fallback:* none needed for correctness — without batch, the design is the interactive
    half only, which is what the spec originally asked for.
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
- **Slack notifications.** A repo issue per the spec's Q&A. `notify.send` is a function, so
  the second channel is a second function, not an interface change.
- ~~**Batch jobs for genuinely long work.**~~ **Now in scope**, at the review's request and
  because the one-interactive-allocation cap makes it necessary rather than optional. See
  *Two execution modes*. This **supersedes the spec's Q&A note** that batch was a
  post-ship feature request; the spec should be amended rather than left contradicting the
  design.
- **What a remote agent should conclude.** Run layout, metrics and analysis stay owned by
  the repo running the experiment (spec, *Out of scope*). This repo gets the compute,
  launches the agent, and stops.

## Appendix C — two rulings the review needs from you

Both of these reverse something the approved spec says, so they are yours to decide rather
than mine to assume. The design as written takes the conservative branch of each.

- **Should the *scheduled* monitor be able to submit batch jobs?**
  - *The spec says no:* "Acting on the cluster from the monitor. The scheduled job reports
    and alerts. It never cancels a job, resizes an allocation, or keeps one alive."
  - *The review said:* "let the monitor agent fire a batch slurm script … overnight".
  - *What the design does:* reads "monitor agent" as the **manager** — the supervising
    session, which already holds kill authority — and gives it `agent-batch`. The
    **scheduled** usage monitor gains only a *report* on how batch jobs went, and still
    never acts. That keeps one actor with cluster authority instead of two.
  - *If you meant the scheduled job literally:* say so and it becomes a small addition —
    but it puts submission authority in an unattended cron job, which is the thing the
    spec's out-of-scope bullet was protecting against.
- **May a secret live on Tillicum so agents can email/Slack you directly?**
  - *The spec says no:* "This repo never writes secrets to the shared filesystem."
  - *The review said:* "have the experiment agents themselves push emails/slack messages to
    me when they are done".
  - *What the design does:* the agent announces completion **on its PR** through the git
    credential it already has there, and the local side emails only about failures and
    silences. No new secret, and the human still hears about everything that matters.
  - *If you want real email from the node:* it needs an SMTP app-password or a Slack webhook
    on the shared filesystem. That is a deliberate reversal of a spec bullet, and worth
    doing only if PR notifications turn out to be too quiet in practice — which we will
    know after a week of real runs.

## Approval

Reviewer: leave line comments on anything above. On approval I will check `Design Approved`
on the task, switch to **Code Implementation**, load **Semantic PRs**, and start at
`…-01-scaffold`.
