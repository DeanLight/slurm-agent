"""Shared fixtures. The whole suite injects at one seam: `Runner`.

`fake_runner` is a dict-backed `Runner`, which is why this repo needs no mocking library:
every function that touches the cluster takes a runner, so a test hands it a mapping from
command substring to canned output and asserts on what was asked.
"""

from collections.abc import Callable

import pytest


class FakeRunner:
    """A `Runner` that answers from a mapping and records what it was asked."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.commands: list[str] = []

    def __call__(self, command: str, stdin: str | None = None) -> str:
        self.commands.append(command)
        for needle, output in self.responses.items():
            if needle in command:
                return output
        return ""

    def asked(self, needle: str) -> bool:
        """Did any command contain `needle`?"""
        return any(needle in c for c in self.commands)


@pytest.fixture
def fake_runner() -> Callable[..., FakeRunner]:
    """Build a `FakeRunner` from a mapping of command substring to stdout."""
    return FakeRunner
