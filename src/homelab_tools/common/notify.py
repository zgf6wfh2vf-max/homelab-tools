"""Send push notifications through ntfy (https://ntfy.sh or a self-hosted server).

Notifications are best-effort: a failure is logged and swallowed, because a monitor must never crash
just because the phone-alert service is unreachable.
"""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)

#: Signature of the function that performs the HTTP request; replaced by a fake in tests.
Opener = Callable[[urllib.request.Request, float], object]


def _urlopen(request: urllib.request.Request, timeout: float) -> object:
    """Default opener: a real HTTP request."""
    return urllib.request.urlopen(request, timeout=timeout)


class Notifier:
    """Posts messages to one ntfy topic.

    Args:
        url: Full topic URL. An empty string makes every :meth:`send` a no-op, so tools can treat
            alerts as optional without ``if`` checks everywhere.
        opener: HTTP function, injectable for tests.
    """

    def __init__(self, url: str, opener: Opener = _urlopen) -> None:
        """Store the topic URL and HTTP function."""
        self.url = url
        self._open = opener

    @property
    def enabled(self) -> bool:
        """Whether a topic is configured."""
        return bool(self.url)

    def send(self, title: str, message: str, *, priority: str = "default", tags: str = "") -> bool:
        """Send one notification.

        Args:
            title: Short headline shown in bold on the phone.
            message: Body text.
            priority: ntfy priority: ``min``, ``low``, ``default``, ``high`` or ``urgent``.
            tags: Comma-separated ntfy tags; emoji short codes (e.g. ``warning``) become icons.

        Returns:
            True if the message was accepted, False if disabled or the request failed.
        """
        if not self.enabled:
            return False
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        request = urllib.request.Request(self.url, data=message.encode(), method="POST", headers=headers)
        try:
            self._open(request, 20)
        except OSError as exc:
            log.warning("ntfy notification %r failed: %s", title, exc)
            return False
        return True
