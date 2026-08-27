# slurm-agent

## Setup

```bash
uv sync      # install dependencies
poe init     # install git hooks
poe nb       # generate .ipynb notebooks from .py source files
poe skill    # teach Claude Code the juplit workflow (re-run after upgrading juplit)
```

## Workflow

| Command | What it does |
|---|---|
| `poe nb` | Generate `.ipynb` files from `.py` sources (run after cloning) |
| `poe sync` | Sync `.py` <-> `.ipynb` after editing |
| `poe clean` | Sync then delete all `.ipynb` files |
| `poe test` | Run tests |
| `poe check` | Fail if a committed notebook's outputs contradict its `.py` |
| `poe html <nb>` | Render a notebook to standalone HTML |
| `poe skill` | Install/refresh the juplit skill so Claude Code knows the workflow |

### Editing notebooks

1. Edit `.py` files directly — these are the source of truth.
2. Run `poe sync` to propagate changes to `.ipynb` notebooks.
3. Commit only `.py` files (`.ipynb` files are gitignored).

### Artifact notebooks

Most `.ipynb` files here are disposable. A notebook whose *outputs* are the deliverable —
an experiment run whose plots and tables are what a reviewer reads — is different: declare
it and both halves are committed and maintained.

```toml
[tool.juplit]
artifact_notebooks = ["experiments/**/*.py"]
```

```gitignore
!experiments/ablation.ipynb   # or the outputs are preserved locally and never committed
```

`poe check` then fails whenever a committed notebook's outputs no longer match the code
that produced them — it runs in CI and as a pre-commit hook. `juplit run <nb> --stale`
re-executes just the cells that drifted. See the
[artifact notebooks tutorial](https://deanlight.github.io/juplit/artifact_notebooks/).
