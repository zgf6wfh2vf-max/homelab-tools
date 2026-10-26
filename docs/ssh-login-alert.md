# ssh-login-alert

A push notification for **every SSH login** to a server. At home, every login should be you, so an
alert for each one means a stolen key or password is noticed the first time it's used.

```
SSH login on ajax
admin logged in from 100.101.102.103 (phone) at 12:03 on Mon 26 Oct.
```

`scripts/ssh-login-alert` is a short bash script that `sshd` runs through PAM (`pam_exec`) when a
session opens:

* **It never gets in the way.** The PAM line is `optional`, and the notification is sent in the
  background with a 10-second timeout, so a slow or unreachable ntfy server can't delay or block a
  login.
* **Allow-list for automation.** Sources in `SSH_ALERT_ALLOW` don't alert. Use it for a machine that
  runs scripts over SSH all day. Anything that can log in from that machine can then do so
  silently, so keep the list short.
* **Labels.** `SSH_ALERT_NAMES` turns addresses into names in the message, such as `phone` for its
  Tailscale address.

## Install

```sh
sudo install -m 755 scripts/ssh-login-alert /usr/local/sbin/
sudo tee /etc/default/ssh-login-alert >/dev/null <<'CONF'
NTFY_URL=https://ntfy.sh/CHANGE-ME-to-a-long-random-topic
SSH_ALERT_ALLOW=""
SSH_ALERT_NAMES=""
CONF
sudo chmod 640 /etc/default/ssh-login-alert
echo 'session optional pam_exec.so quiet /usr/local/sbin/ssh-login-alert' | sudo tee -a /etc/pam.d/sshd
```

Keep an existing SSH session open while you test. `sshd` reads the PAM file on each login, so no
restart is needed.

## Settings

| Setting | Meaning |
|---|---|
| `NTFY_URL` | ntfy topic URL; nothing is sent without it |
| `SSH_ALERT_ALLOW` | Space-separated sources that don't alert: exact addresses, or prefixes ending in `.` or `:` |
| `SSH_ALERT_NAMES` | Space-separated `address=label` pairs; write spaces in a label as `_` (`Office_PC`) |
| `SSH_ALERT_HOST` | Name in the title (default: the hostname) |

Failed attempts aren't reported here. With no ports open to the internet, they can only come from
inside your network or tailnet. A honeypot such as [OpenCanary](opencanary-ntfy.md) is a better way
to catch something probing the network.
