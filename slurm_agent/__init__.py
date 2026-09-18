"""Local Claude Code orchestration of Tillicum jobs and remote agents."""

import sys

import structlog

__version__ = "0.1.0"

# Logs go to STDERR, not stdout. structlog's default is stdout, which quietly breaks the
# one contract this repo has with a shell: `TASK=$(poe task-new "…")` must capture a task
# id and nothing else. A log line landing in that variable does not fail — it launches an
# agent against a task named after a timestamp. Diagnostics are not output.
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
