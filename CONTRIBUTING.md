# Contributing

Thanks for helping! Bug reports, register maps for other inverter models and new tools are welcome.

## Ground rules

* **Read-only.** Tools must never change a device's or account's settings. Write calls need a very
  good reason and must be opt-in.
* **No secrets in code or examples.** Settings come from the environment (see `common/config.py`);
  examples use placeholders like `CHANGE-ME` and private example addresses.
* **Logic in pure functions, I/O at the edges**, so it can be unit-tested without hardware or network.
* **Fail quietly, alert once.** Offline devices are logged and retried; alerts are de-duplicated.

## Development setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Before opening a pull request

```sh
.venv/bin/ruff format .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
shellcheck scripts/* examples/keepalived/check-pihole-dns examples/keepalived/keepalived-notify-ntfy
```

CI runs the same checks. Please:

* give every module, class and function a docstring (Google style),
* add or update tests - fixtures built from real device responses (anonymised) are ideal,
* update the tool's page in `docs/` (a test checks that documented status fields match the code),
* add a line to `CHANGELOG.md`.
