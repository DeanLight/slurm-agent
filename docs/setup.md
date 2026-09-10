# Setting up a new machine

This repo only works from the laptop that can reach Tillicum. Everything below happens
there, once.

```bash
git clone https://github.com/DeanLight/slurm-agent && cd slurm-agent
uv sync --all-groups
poe hooks          # git pre-commit hooks
poe init           # create the footprint, then prove it works
```

[`docs/quickstart.ipynb`](quickstart.py) is this page as a notebook you actually run, and
it goes further: past setup into two real trial agents, one interactive and one batch. Use
it on a new machine and read this page when a step needs explaining.

```bash
poe nb             # pair the notebooks, including the quick start
```

## Three places, and which one a message is about

Nearly every confusion here is a missing thing whose *machine* was not stated. There are
exactly three kinds of place, and every row of every report names the one it means:

| Place | What lives there | Its `.envrc` |
|---|---|---|
| **This laptop** | this checkout, `~/.ssh/config`, the supervision loop | the keys for reaching **you** — SMTP, Slack |
| **The login node** | the run root, `tmux`, your Claude credential | none — there is no `.envrc` here |
| **Each staged repo on the cluster** | the repo one agent runs in | the keys **that agent** declares, e.g. `HF_TOKEN` |

So `HF_TOKEN` is never a laptop problem, and `poe hc` does not ask the laptop for one.

**How it knows which repos it manages:** it reads `agents/*.yaml`, one file per agent, and
nothing else. Each file names a repo, a ref and the workdir it is staged into — that is the
whole list. No registry, nothing remembered between runs; delete a file and it stops
managing that repo. `poe init` prints the list before doing anything.

`poe init` **appends** its hosts to `~/.ssh/config` between markers, and skips entirely if
you have already defined `tillicum-login` yourself. Your other clusters and servers are
never touched, and nothing there is overwritten.

Allocations are held in `tmux` on the login node by default, so `poe healthcheck` checks
tmux is installed there. Set `allocation_mode: no_shell` in `config/cluster.yaml` if it is
not — you lose the ability to attach to a running allocation.

`poe init` creates what it safely can — `.envrc` from the template at mode 0600, the ssh
config entries, the run root on the cluster — and then runs a full healthcheck that
**really sends** a test email and Slack message, from your laptop *and* from Tillicum. A
clone is not set up until both arrive.

It will fail the first time, and that is correct: the `.envrc` it just wrote is full of
`<secret-here>` placeholders. Fill them in.

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

For email you want an **app password**, not your account password. For Slack you want an
[incoming webhook](https://api.slack.com/messaging/webhooks) URL.

## A different `.envrc` per staged repo, on Tillicum

Remote agents read their keys — and send their notifications — from the compute node, so
each staged repo needs its own file, holding exactly what that agent's `requires_env`
declares. `poe hc` prints the exact path in the heading above the row, so there is nothing
to work out.

```bash
scp templates/envrc.example tillicum-login:~/work/<repo>/.envrc
ssh tillicum-login 'chmod 600 ~/work/<repo>/.envrc && $EDITOR ~/work/<repo>/.envrc'
```

`poe hc` checks that file's mode and keys too, and fails loudly if it is group-readable —
Tillicum's filesystem is shared, and a 0644 app password is the real exposure here. An
agent that declares no keys says `declares no keys — nothing needed here` instead, and
there is nothing to copy for it.

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
poe monitor-install     # 0 9 */3 * * — a digest at most every 3 days, only if spend moved
poe monitor-status
poe monitor-run --dry-run
```

Silence means nothing changed. A message always means something did.
