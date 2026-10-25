"""Write small JSON status files for dashboards.

A dashboard such as Homepage (https://gethomepage.dev) can show these through its ``customapi``
widget if a web server publishes the directory (the examples use a read-only busybox httpd).
Values are display strings ("21.5 °C", "£2.54") so the dashboard needs no formatting logic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path


def write_status(directory: str, name: str, data: Mapping[str, object], mode: int = 0o644) -> Path | None:
    """Atomically write ``data`` as ``<directory>/<name>.json``.

    The file is written to a temporary name and renamed into place, so a reader never sees a
    half-written file.

    Args:
        directory: Target directory; an empty string disables writing.
        name: File name without the ``.json`` suffix.
        data: JSON-serialisable mapping.
        mode: File permissions (status files hold no secrets and are world-readable by default).

    Returns:
        The path written, or ``None`` when disabled.
    """
    if not directory:
        return None
    path = Path(directory) / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.chmod(mode)
    tmp.replace(path)
    return path
