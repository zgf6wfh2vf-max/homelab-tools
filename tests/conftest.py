"""Shared pytest fixtures."""

from __future__ import annotations

import os
import time
import urllib.request
from dataclasses import dataclass, field

import pytest

# The tools work in the host's local time and are written for a UK host, so the tests run in UK time
# whatever zone the machine is in. Set here, at import, so module-level times in test files use it too.
os.environ["TZ"] = "Europe/London"
time.tzset()


@dataclass
class FakeHTTP:
    """Records requests instead of sending them; optionally fails every call."""

    fail: bool = False
    requests: list[urllib.request.Request] = field(default_factory=list)

    def __call__(self, request: urllib.request.Request, timeout: float) -> object:
        self.requests.append(request)
        if self.fail:
            raise OSError("network down")
        return object()

    def bodies(self) -> list[str]:
        return [r.data.decode() for r in self.requests if isinstance(r.data, bytes)]


@pytest.fixture
def http() -> FakeHTTP:
    return FakeHTTP()
