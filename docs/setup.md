# Setting up a new machine

This repo only works from the laptop that can reach Tillicum. Everything below happens
there, once.

```bash
git clone https://github.com/DeanLight/slurm-agent && cd slurm-agent
uv sync --all-groups
poe hooks          # git pre-commit hooks
poe init           # create the footprint, then prove it works
```

[`docs/quickstart.ipynb`](quickstart.py) is this page as a notebook you actually run: two
commands, then the `claude` commands to type. Use it on a new machine and read this page
when a step needs explaining.

The work itself you never drive by hand, and a task id is the whole interface. Open Claude
Code in this repo and say *"pick up TASK-118 on Tillicum, keep me posted on progress and
spend"* —
`.claude/skills/slurm-orchestration/SKILL.md` auto-loads and takes the whole job: opening
the Notion tasks, sizing the compute, launching, supervising, reporting, and tearing down.

`claude -p "…"` is the same manager from a script or a notebook — the same command, just
printing its reply and exiting. `--output-format` defaults to `text`, so stdout is the
reply and `IDS=$(claude -p '…')` feeds one session into the next.

```bash
poe nb             # pair the notebooks, including the quick start
```

## Three places, and which one a message is about

Nearly every confusion here is a missing thing whose *machine* was not stated. There are
exactly three kinds of place, and every row of every report names the one it means:

| Place | What lives there | Its `.envrc` |
|---|---|---|
| **This laptop** | this checkout, `~/.ssh/config`, the manager and the supervision loop | nothing in the shipped repo — `config/manager.yaml` declares no keys |
| **The login node** | the run root, `tmux`, your Claude credential | none — there is no `.envrc` here |
| **Each staged repo on the cluster** | the repo one agent runs in | the keys **that agent** declares, e.g. `HF_TOKEN` |

So `HF_TOKEN` is never a laptop problem, and `poe hc` does not ask the laptop for one.

**How it knows which repos it manages:** it reads `agents/*.yaml`, one file per agent, and
nothing else. Each file names a repo, a ref and the workdir it is staged into — that is the
whole list. No registry, nothing remembered between runs; delete a file and it stops
managing that repo. You never ask separately: the report heads one group per agent, so
the list is wherever the answer is needed.

**Every shipped agent points at this repo and declares no keys**, so a fresh clone or fork
is green with nothing filled in and nothing staged yet.
`agents/experiment-runner.yaml` is a placeholder in that sense — repoint its `repo`, `ref`
and `workdir` at your experiment repo, and declare what it needs there. The two trial-task
agents are meant to stay pointed here: a sanity check should not depend on access to
anything but this repo, and they only ever *read* what they stage — their notebooks go
under the run root, so nothing is pushed and there is no pull request to review.

`poe init` **appends** its hosts to `~/.ssh/config` between markers, and skips entirely if
you have already defined `tillicum-login` yourself. Your other clusters and servers are
never touched, and nothing there is overwritten.

Allocations are held in `tmux` on the login node by default, so `poe healthcheck` checks
tmux is installed there. Set `allocation_mode: no_shell` in `config/cluster.yaml` if it is
not — you lose the ability to attach to a running allocation.

`poe init` creates what it safely can — `.envrc` from the template at mode 0600, the ssh
config entries, the run root on the cluster — and then prints **one** report of everything,
grouped by machine. Creation itself says nothing: what exists afterwards is the report's to
state, and what was just created rides along inside that row as a `·` note.

Its full tier spends a few cents proving the expensive things: a real headless `claude -p`
on each machine, and one per MCP server per machine. Being logged in to Claude and having
an authorised Notion grant are separate facts that expire separately, and an agent that
cannot read its task is an allocation spent on nothing.

## Filling in this laptop's `.envrc`

`.envrc` is gitignored and holds the real values. `templates/envrc.example` is the
committed placeholder version, and `config/manager.yaml` plus each `agents/*.yaml` name
the keys — **never the values**.

The file `poe init` writes is not a copy of the template. It carries a header written for
*that* file — what it is, that it is read on this laptop only, which keys it holds — and
any key that is only ever read on the cluster is written **commented out**, annotated with
the remote path it belongs in. Filling one of those in here changes nothing.

```bash
$EDITOR .envrc     # replace every <secret-here> that is NOT commented out
chmod 600 .envrc   # poe init does this, but check after editing
poe hc             # says which keys are still missing, and on which machine
```

**In the shipped repo there is nothing to fill in here**, and `my keys` is green with
nothing to demand. The manager needs no credential to reach you: it reaches you by being a
session you are talking to. Start it with `claude --remote-control` and that conversation
is also at claude.ai/code and in the Claude app — no SMTP host, no webhook, nothing that
can silently stop delivering.

A key appears under `this laptop` only when `config/manager.yaml` declares one, which is
what a fork does if the manager itself reads something.

## A different `.envrc` per staged repo, on Tillicum

Remote agents read their keys on the compute node, so each staged repo needs its own file,
holding exactly what that agent's `requires_env` declares. `poe hc` prints the exact path in the heading above the row, so there is nothing
to work out.

```bash
scp templates/envrc.example tillicum-login:~/work/<repo>/.envrc
ssh tillicum-login 'chmod 600 ~/work/<repo>/.envrc && $EDITOR ~/work/<repo>/.envrc'
```

`poe hc` checks that file's mode and keys too, and fails loudly if it is group-readable —
Tillicum's filesystem is shared, and a 0644 app password is the real exposure here. An
agent that declares no keys says `declares no keys — nothing needed here` instead, and
there is nothing to copy for it.

## GitHub credentials, on both machines

A real experiment agent pushes its write-up from the compute node, so the cluster needs
its own credential (`gh auth login` there, or a PAT in git's credential store). Your laptop
needs one too. The trial tasks do not push at all — which is exactly why `hc` proves this
rather than leaving it to them. `poe hc`
checks both — look for the `github …` rows under each heading — because they are different
credentials and only one of them is the one that matters at the moment it matters.

`hc --full` adds a `--dry-run` push, which is the only thing that proves **write** access:
`git ls-remote` succeeds on a public repo with no credential at all. The dry run
authenticates, is authorised by the server, and then writes nothing.

A staged repo reading `not cloned yet` is not a problem. A workdir is created by the first
`poe agent-run`/`agent-batch`, not by setup.

## Authenticating Claude on the cluster

Remote agents run under your Claude subscription, logged in on Tillicum:

```bash
ssh tillicum-login
claude          # log in once, interactively
```

`poe hc --full` confirms it works headlessly *and* that it reports a non-zero cost — a
subscription reporting zero would silently disarm `--max-budget-usd`, and nothing else
would notice.

## The usage digest

```bash
poe monitor-install     # 0 9 */3 * * — a reading at most every 3 days
poe monitor-status
poe monitor-run --dry-run
poe spend               # what those readings recorded — the manager reads this too
```

It records; it does not deliver. Every poll appends to `usage.jsonl` under the run root
**on the cluster**, and only a poll with news is marked a digest — so `poe spend` can tell
"spend has not moved" apart from "nobody has looked", which is the distinction a cron entry
that quietly died destroys.

The ledger lives on the cluster rather than beside this checkout for the same reason
everything else does: a reinstall would otherwise lose the spend history, and two sessions
would disagree about it. The poll is already ssh'd in to run `hyakusage`, so writing the
answer back costs one more command. When the tunnel is down nothing is polled and nothing
is written, and the gap is the honest record.

Nothing is sent anywhere. Ask the manager what something cost and it reads this.
