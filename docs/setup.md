# Setting up a new machine

This repo only works from the laptop that can reach Tillicum. Everything below happens
there, once.

```bash
git clone https://github.com/DeanLight/slurm-agent && cd slurm-agent
uv sync --all-groups
poe hooks          # git pre-commit hooks
poe init           # create the footprint, then prove it works
```

`poe init` creates what it safely can — `.envrc` from the template at mode 0600, the ssh
config entries, the run root on the cluster — and then runs a full healthcheck that
**really sends** a test email and Slack message, from your laptop *and* from Tillicum. A
clone is not set up until both arrive.

It will fail the first time, and that is correct: the `.envrc` it just wrote is full of
`<secret-here>` placeholders. Fill them in.

## Filling in `.envrc`

`.envrc` is gitignored and holds the real values. `templates/envrc.example` is the
committed placeholder version, and `config/manager.yaml` plus each `agents/*.yaml` name
the keys — **never the values**.

```bash
$EDITOR .envrc     # replace every <secret-here>
chmod 600 .envrc   # poe init does this, but check after editing
poe hc             # confirms every declared key is set and filled in
```

For email you want an **app password**, not your account password. For Slack you want an
[incoming webhook](https://api.slack.com/messaging/webhooks) URL.

## The same file on Tillicum

Remote agents send from the compute node, so they need their own copy — in the `.envrc` of
the repo they run in, which is what `requires_env` in that agent's config refers to.

```bash
scp templates/envrc.example tillicum-login:~/work/<repo>/.envrc
ssh tillicum-login 'chmod 600 ~/work/<repo>/.envrc && $EDITOR ~/work/<repo>/.envrc'
```

`poe hc` checks that file's mode and keys too, and fails loudly if it is group-readable —
Tillicum's filesystem is shared, and a 0644 app password is the real exposure here.

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
