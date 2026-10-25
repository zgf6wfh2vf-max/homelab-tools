# Security

## Reporting

Please report security issues privately (GitHub's "Report a vulnerability" on this repository)
rather than in a public issue.

## Things to know when running these tools

* **ntfy topics are not secret by design.** Anyone who knows a topic on ntfy.sh can read its
  messages and post to it. Use a long random topic, or a self-hosted ntfy with access control.
* **Credentials** (Octopus API key, Pod Point password, tado refresh token) should be in files owned
  by root, group of the service user, mode 640 (tokens the service writes itself: 600).
* **Status JSON and Grafana embeds** are meant for a trusted home network. Do not expose them to
  the internet without authentication: they reveal when you are home, your energy use and more.
* The tools only read. The honeypot (OpenCanary) deliberately attracts connections - keep it on an
  address with nothing else on it.
