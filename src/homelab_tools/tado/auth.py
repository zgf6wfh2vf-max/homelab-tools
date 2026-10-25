"""tado login: OAuth 2.0 device authorisation flow with rotating refresh tokens.

Since March 2025 tado only accepts this flow for third-party tools:

1. ``tado-auth`` asks tado for a device code and prints a URL. You open it, log in and approve.
   **The code expires after five minutes.**
2. tado returns a refresh token, stored in ``TADO_TOKEN_FILE``.
3. ``tado-monitor`` swaps the refresh token for a 10-minute access token whenever needed. tado
   *rotates* refresh tokens: every refresh returns a new one and the old one stops working, so the
   new token is saved immediately, atomically. If the file is ever lost or stale, run ``tado-auth``
   again.

The client id is the public one tado documents for device-flow clients.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from homelab_tools.common.config import env_str

CLIENT_ID = (
    "1bb50063-6b0c-4d11-bd99-387f4a91cc46"  # public device-flow client id, not a secret  # gitleaks:allow
)
AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
DEFAULT_TOKEN_FILE = "/var/lib/tado-monitor/token.json"

#: POSTs form data and returns the decoded JSON (error bodies included); injectable for tests.
Poster = Callable[[str, dict[str, str]], dict[str, Any]]


def http_post(url: str, form: dict[str, str]) -> dict[str, Any]:
    """POST a form and return the JSON body, also for HTTP error statuses (OAuth errors are JSON)."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            body: dict[str, Any] = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = json.load(exc)
    return body


class TokenStore:
    """Keeps the current refresh token in a private JSON file."""

    def __init__(self, path: str | Path) -> None:
        """Remember where the token lives."""
        self.path = Path(path)

    def load(self) -> str:
        """Return the stored refresh token. Raises ``OSError``/``KeyError`` if there is none."""
        token: str = json.loads(self.path.read_text())["refresh_token"]
        return token

    def save(self, refresh_token: str) -> None:
        """Atomically replace the stored token (mode 600: it grants access to your heating)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"refresh_token": refresh_token, "saved": time.time()}, f)
        tmp.replace(self.path)


class AccessToken:
    """Hands out a valid access token, refreshing (and re-saving the rotated refresh token) as needed."""

    def __init__(
        self, store: TokenStore, post: Poster = http_post, clock: Callable[[], float] = time.time
    ) -> None:
        """Wrap a token store."""
        self.store, self._post, self._clock = store, post, clock
        self._token, self._expires = "", 0.0

    def get(self) -> str:
        """Return an access token valid for at least another minute."""
        if self._clock() < self._expires - 60:
            return self._token
        reply = self._post(
            TOKEN_URL,
            {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": self.store.load()},
        )
        if "access_token" not in reply:
            raise PermissionError(
                f"tado refused the refresh token ({reply.get('error')}); run tado-auth again"
            )
        self.store.save(reply["refresh_token"])
        self._token = reply["access_token"]
        self._expires = self._clock() + float(reply.get("expires_in", 600))
        return self._token


def device_login(
    store: TokenStore,
    post: Poster = http_post,
    show: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Run the device flow: show the approval URL, wait for approval and save the refresh token.

    Returns:
        True once approved; False if the code expired or tado reported an error.
    """
    start = post(AUTH_URL, {"client_id": CLIENT_ID, "scope": "offline_access"})
    show(
        f"Open this within {start['expires_in'] // 60} minutes and approve: "
        f"{start['verification_uri_complete']}"
    )
    show(f"(the page should show the code {start['user_code']})")
    deadline = clock() + start["expires_in"]
    interval = float(start.get("interval", 5))
    while clock() < deadline:
        sleep(interval)
        reply = post(
            TOKEN_URL,
            {
                "client_id": CLIENT_ID,
                "device_code": start["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if "refresh_token" in reply:
            store.save(reply["refresh_token"])
            show("Approved - token saved.")
            return True
        if reply.get("error") == "slow_down":
            interval += 5
        elif reply.get("error") != "authorization_pending":
            show(f"Login failed: {reply.get('error_description') or reply.get('error')}")
            return False
    show("The code expired before it was approved; run tado-auth again.")
    return False


def main() -> None:
    """Entry point for ``tado-auth``."""
    store = TokenStore(env_str("TADO_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    sys.exit(0 if device_login(store, show=lambda line: print(line, flush=True)) else 1)


if __name__ == "__main__":
    main()
