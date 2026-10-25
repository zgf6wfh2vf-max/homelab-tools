"""Read settings from environment variables.

Tools are configured only through the environment (typically a systemd ``EnvironmentFile=``), so the
source code never contains hosts, tokens or topic names. These helpers give clear errors for missing
or malformed values instead of a ``KeyError`` deep inside a tool.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """A required setting is missing or has an invalid value."""


def env_str(name: str, default: str | None = None, *, env: Mapping[str, str] | None = None) -> str:
    """Return the string value of ``name``.

    Args:
        name: Environment variable name.
        default: Value to use when the variable is unset. ``None`` makes the setting required.
        env: Mapping to read from instead of :data:`os.environ` (used by tests).

    Raises:
        ConfigError: If the variable is unset and no default was given.
    """
    source = os.environ if env is None else env
    value = source.get(name)
    if value is None or value == "":
        if default is None:
            raise ConfigError(f"required setting {name} is not set")
        return default
    return value


def env_int(name: str, default: int | None = None, *, env: Mapping[str, str] | None = None) -> int:
    """Return the integer value of ``name`` (see :func:`env_str` for the arguments).

    Raises:
        ConfigError: If the variable is required and unset, or is not a whole number.
    """
    raw = env_str(name, None if default is None else str(default), env=env)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"setting {name} must be a whole number, got {raw!r}") from exc


def env_float(name: str, default: float | None = None, *, env: Mapping[str, str] | None = None) -> float:
    """Return the float value of ``name`` (see :func:`env_str` for the arguments).

    Raises:
        ConfigError: If the variable is required and unset, or is not a number.
    """
    raw = env_str(name, None if default is None else str(default), env=env)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"setting {name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Outputs:
    """Where a tool publishes its results. Every output is optional.

    Attributes:
        ntfy_url: Full ntfy topic URL (e.g. ``https://ntfy.sh/<secret-topic>``); empty disables alerts.
            Treat the topic name like a password: anyone who knows it can read and send messages.
        vm_url: Base URL of VictoriaMetrics (e.g. ``http://127.0.0.1:8428``); empty disables metrics.
        status_dir: Directory for status JSON files read by dashboards; empty disables them.
    """

    ntfy_url: str
    vm_url: str
    status_dir: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Outputs:
        """Build from ``NTFY_URL``, ``VM_URL`` and ``STATUS_DIR``."""
        return cls(
            ntfy_url=env_str("NTFY_URL", "", env=env),
            vm_url=env_str("VM_URL", "", env=env).rstrip("/"),
            status_dir=env_str("STATUS_DIR", "", env=env),
        )
