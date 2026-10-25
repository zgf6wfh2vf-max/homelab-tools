"""Shared pytest fixtures."""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field

import pytest


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
