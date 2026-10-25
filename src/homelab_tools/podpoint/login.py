"""Keep one Pod Point sign-in alive instead of signing in again every hour.

Why this exists: ``podpointclient`` refreshes its (one-hour) access token by itself, but on every
refresh it also POSTs your email and password to Pod Point's ``/sessions`` endpoint to create a
*new* session. Pod Point treats each one as a new sign-in and emails you about it - roughly 24
emails a day for a monitor that polls around the clock.

Only two things are needed after the first sign-in:

* the Google **refresh token**, which renews the access token without a password, and
* the Pod Point **user id** (and session id) that the first session returned; they do not change.

:class:`LoginCache` stores those in a private file, and :func:`cached_session_class` makes the
library reuse them instead of creating a new session. Result: one sign-in when the cache is first
created (or after Pod Point rejects the saved token), and none on hourly refreshes or restarts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class LoginCache:
    """The parts of a Pod Point sign-in worth keeping, persisted in a mode-600 JSON file.

    Attributes:
        path: Cache file location (inside the service's private state directory).
        refresh_token: Google refresh token from the first sign-in.
        user_id: Pod Point user id from the first session.
        session_id: Pod Point session id from the first session.
    """

    path: Path
    refresh_token: str = ""
    user_id: str = ""
    session_id: str = ""

    @classmethod
    def load(cls, path: Path) -> LoginCache:
        """Read the cache; an empty cache if the file is missing or unreadable."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls(path)
        return cls(
            path,
            str(data.get("refresh_token", "")),
            str(data.get("user_id", "")),
            str(data.get("session_id", "")),
        )

    @property
    def complete(self) -> bool:
        """Whether everything needed to skip signing in is present."""
        return bool(self.refresh_token and self.user_id)

    def save(self) -> None:
        """Write the cache atomically with mode 600 (the refresh token grants account access)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {"refresh_token": self.refresh_token, "user_id": self.user_id, "session_id": self.session_id},
                f,
            )
        tmp.replace(self.path)

    def clear(self) -> None:
        """Forget everything (after Pod Point rejected the saved token), so the next poll signs in."""
        self.refresh_token = self.user_id = self.session_id = ""
        self.path.unlink(missing_ok=True)


def cached_session_class(base: type[Any], cache: LoginCache) -> type[Any]:
    """Return a subclass of the library's ``Session`` whose ``create()`` reuses the cached session.

    Args:
        base: ``podpointclient.helpers.session.Session`` (any class with an async ``create()`` that
            sets ``user_id`` and ``session_id`` works - tests pass a fake).
        cache: Where the first session's ids are stored.
    """

    class CachedSession(base):  # type: ignore[misc]
        """Creates a Pod Point session only when none is cached."""

        async def create(self) -> bool:
            if cache.user_id:
                self.user_id, self.session_id = cache.user_id, cache.session_id
                return True
            created = bool(await super().create())
            if created:
                cache.user_id, cache.session_id = str(self.user_id), str(self.session_id)
                cache.save()
            return created

    return CachedSession


def restore(auth: Any, cache: LoginCache) -> bool:
    """Prime the library's ``Auth`` object from the cache so its next call refreshes instead of signing in.

    The access token is marked as already expired; the library then exchanges the cached refresh
    token for a new one (no password involved) and, through :func:`cached_session_class`, reuses
    the cached session.

    Returns:
        True if the cache was complete and has been applied.
    """
    if not cache.complete:
        return False
    auth.refresh_token = cache.refresh_token
    auth.access_token = "expired"  # any non-empty value; the expiry below forces a refresh
    auth.access_token_expiry = datetime.now() - timedelta(seconds=1)  # the library compares naive local times
    return True


def remember(auth: Any, cache: LoginCache) -> None:
    """Save the library's current refresh token if it changed (call after successful API calls)."""
    token = getattr(auth, "refresh_token", None)
    if token and token != cache.refresh_token and cache.user_id:
        cache.refresh_token = token
        cache.save()
