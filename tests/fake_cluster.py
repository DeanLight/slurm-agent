"""A fake Tillicum, good enough to run every command against.

The point is not fidelity. It is that **every `slurm-agent` command can be executed here**,
end to end, through the real CLI and the real config files — so the bugs that have been
escaping to a real laptop stop escaping. Those were: a stale config the models no longer
accepted, a log line on stdout, and an unquoted `|` that the remote shell read as a pipe.
None needed a cluster to find. All three needed something to actually *run the command*.

Two things make it catch more than a dict of canned strings:

* **Every command is syntax-checked with `bash -n`** before it is answered. That is what an
  ssh'd command has to survive, and it is exactly what `--Format=StepID:|` failed. A fake
  that only pattern-matched would have answered it happily.
* **Answers are matched against the whole command**, most specific first, and an unmatched
  command is recorded rather than silently returning "". A command nobody anticipated shows
  up as an empty answer in a failing assertion instead of a passing test.
"""

import json
import re
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Ordered: first match wins, so put the specific patterns above the general ones.
# `name|job_id|node|state|left|used|tres` is what `jobs.SQUEUE_FORMAT` asks for.
ANSWERS: list[tuple[str, str]] = [
    # The one round trip everything that polls goes through: `probe.sh` piped to `sh -s`.
    (r"sh -s ", FIXTURES.joinpath("probe.json").read_text()),
    # step id | step name — `launch._agents_on` reads the first field, `watch._step_of`
    # matches the session's short id in the second.
    (r"squeue --job=\S+ --steps", "62526.0|sa-4f2c\n"),
    (r"squeue --me", "dev|295750|g006|R|0:59:00|0:01:00|cpu=8,gres/gpu=1\n"),
    (r"sacct", "295749|dev|COMPLETED|2026-09-15T04:00:00|1200|cpu=8,gres/gpu=1\n"),
    (r"sbatch", "295751;tillicum\n"),
    (r"scancel|tmux kill-session", ""),
    (r"tmux -V", "tmux 3.2a\n"),
    (r"tmux new-session", "held\n"),
    (r"tmux has-session", "held\n"),
    (r"id -un", "deanlcs\n"),
    (r"git -C \S+ remote get-url", "https://github.com/DeanLight/slurm-agent\n"),
    (r"git -C \S+ status --porcelain", ""),
    (r"git -C \S+ rev-parse", "a1b2c3d4e5f6\n"),
    (r"git ls-remote", "a1b2c3d4\trefs/heads/main\n"),
    (r"git .*push --dry-run", "Everything up-to-date\n"),
    (r"stat -c %a", "600\n"),
    (r"cat \S*launch\.json",
     '{"session_id": "4f2cabcd", "task": "TASK-118", "leases_used": 1, "max_leases": 4,'
     ' "agent_kind": "smoke", "job_name": "dev", "log_dir": "/run/4f2cabcd/evidence",'
     ' "run_dir": "~/.slurm-agent/runs/4f2cabcd", "mode": "interactive"}'),
    (r"cat \S*status\.json",
     '{"state": "running", "round": "1/2", "notebook": "/run/4f2cabcd/evidence/smoke.ipynb"}'),
    (r"claude ", '{"result": "Tasks", "total_cost_usd": 0.01, "is_error": false}'),
    (r"juplit cells", "1 markdown · 1 code\n"),
    (r"python3 -c", "ok\n"),
    (r"hyakusage|hyakalloc", FIXTURES.joinpath("hyakusage.txt").read_text()),
    (r"ls |find |tail |mkdir|rm -rf|cp |scp |chmod|setsid|srun|. ./.envrc", ""),
]


class FakeCluster:
    """A `Runner` that answers plausibly — and rejects anything a real shell would."""

    def __init__(self, extra: list[tuple[str, str]] | None = None):
        self.answers = (extra or []) + ANSWERS
        self.commands: list[str] = []
        self.unmatched: list[str] = []

    def __call__(self, command: str, stdin: str | None = None) -> str:
        self.commands.append(command)
        check = subprocess.run(["bash", "-n", "-c", command], capture_output=True, text=True)
        if check.returncode != 0:
            raise AssertionError(
                f"this command is not valid shell, so ssh would reject it:\n"
                f"  {command[:400]}\n  {check.stderr.strip()}")
        for pattern, output in self.answers:
            if re.search(pattern, command):
                return output
        # Most of this repo's remote calls are `<probe> && echo A || echo B` — the shape
        # exists so a missing file is an answer rather than a non-zero exit. Answering the
        # success branch generically beats a pattern per call site, and keeps the fake
        # honest: it cannot drift from a probe it has never seen.
        success = re.search(r"&&\s*echo\s+(\S+)", command)
        if success:
            return success.group(1).strip("{}\"'") + "\n"
        self.unmatched.append(command)
        return ""

    def asked(self, needle: str) -> bool:
        return any(needle in c for c in self.commands)
