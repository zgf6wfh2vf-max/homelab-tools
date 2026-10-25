"""homelab-tools: small, read-only monitors for a home lab.

Every tool follows the same shape:

* it reads a device or service (Modbus, a cloud API, a log file, a SQLite database),
* turns what it reads into plain data with *pure* functions (these are what the unit tests cover),
* and publishes the result through three optional outputs from :mod:`homelab_tools.common`:
  ntfy push notifications, VictoriaMetrics metrics, and a status JSON file for dashboards.

All settings come from environment variables, so the same code runs anywhere and no secret ever
lives in the source. See ``README.md`` and ``docs/`` for each tool.
"""

__version__ = "0.2.0"
