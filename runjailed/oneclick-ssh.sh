#!/usr/bin/env bash
#
# one-click-ssh.sh — enables SSH on a jailbroken-able Roomba (j/i series,
# cleantrack 24.29.1) over its local MQTT broker.
#
# It does everything in one run:
#   1. serves runjailed/scripts over HTTP for the robot
#   2. publishes the sdiscUrl payload through connectivity_manager
#   3. collects the beacons ("Running script on Roomba as root", "Enabled SSH!")
#   4. waits until TCP/22 answers
#
# Usage:
#   ./runjailed/oneclick-ssh.sh <ROBOT_IP> [options]
#
# Options:
#   --blid BLID         robot BLID (falls back to $ROOMBA_BLID, else prompt)
#   --password PW       robot password (prefer $ROOMBA_PASSWORD or the prompt)
#   --host-ip IP        this machine's LAN IP (default: route to the robot)
#   --http-port PORT    port serving runjailed/scripts (default 8899)
#   --beacon-port PORT  local beacon port (default 8883)
#   --ssh-port PORT     port to probe on the robot (default 22)
#   --linger SECS       how long to keep the beacon server up (default 25)
#   --timeout SECS      how long to wait for SSH (default 120)
#   --no-serve          the scripts are already served at HOST:HTTP_PORT (skip the local server)
#   --dry-run           print the payload and exit
#
# Exit codes: 0 SSH reachable, 1 failure, 2 usage, 3 published but SSH closed.

set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SCRIPTS_DIR="$REPO_ROOT/runjailed/scripts"
RUNJAILED_PY="$REPO_ROOT/runjailed/runjailed.py"

ROBOT=""
BLID="${ROOMBA_BLID:-}"
PASSWORD="${ROOMBA_PASSWORD:-}"
HOST_IP=""
HTTP_PORT=8899
BEACON_PORT=8883
SSH_PORT=22
LINGER=0
WAIT=120
SERVE=1
DRY_RUN=0

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --blid)        BLID="$2"; shift 2 ;;
        --password)    PASSWORD="$2"; shift 2 ;;
        --host-ip)     HOST_IP="$2"; shift 2 ;;
        --http-port)   HTTP_PORT="$2"; shift 2 ;;
        --beacon-port) BEACON_PORT="$2"; shift 2 ;;
        --ssh-port)    SSH_PORT="$2"; shift 2 ;;
        --linger)      LINGER="$2"; shift 2 ;;
        --timeout)     WAIT="$2"; shift 2 ;;
        --no-serve)    SERVE=0; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage ;;
        -*)            die "unknown option $1" ;;
        *)             [ -z "$ROBOT" ] || die "unexpected argument $1"; ROBOT="$1"; shift ;;
    esac
done

[ -n "$ROBOT" ] || usage
[ -f "$RUNJAILED_PY" ] || die "runjailed.py not found at $RUNJAILED_PY"

# the beacon server lives inside runjailed.py; keep it up for the whole wait
[ "$LINGER" -gt 0 ] 2>/dev/null || LINGER=$WAIT

# --- python with paho-mqtt ---------------------------------------------------
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for candidate in "$REPO_ROOT/.venv/bin/python" "$(command -v python3 || true)" "$(command -v python || true)"; do
        if [ -n "$candidate" ] && "$candidate" -c 'import paho.mqtt.client' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    done
fi
[ -n "$PY" ] || die "no python with paho-mqtt found (export PYTHON=/path/to/venv/bin/python)"
"$PY" -c 'import paho.mqtt.client' 2>/dev/null || die "$PY has no paho-mqtt"
echo "python       : $PY"

# --- LAN IP of this machine ---------------------------------------------------
if [ -z "$HOST_IP" ]; then
    HOST_IP=$(ip -4 route get "$ROBOT" 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')
fi
[ -n "$HOST_IP" ] || HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$HOST_IP" ] || die "cannot determine this machine's LAN IP; pass --host-ip"

# --- credentials --------------------------------------------------------------
if [ -z "$BLID" ] && [ -t 0 ]; then
    read -r -p "BLID: " BLID
fi
[ -n "$BLID" ] || die "BLID missing (--blid, \$ROOMBA_BLID or interactive)"

if [ -z "$PASSWORD" ] && [ -t 0 ]; then
    read -r -s -p "Robot password: " PASSWORD
    printf '\n'
fi
[ -n "$PASSWORD" ] || die "password missing (\$ROOMBA_PASSWORD, --password or interactive)"
export ROOMBA_BLID="$BLID" ROOMBA_PASSWORD="$PASSWORD"

# --- payload ------------------------------------------------------------------
PAYLOAD=$("$PY" -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('runjailed', sys.argv[4])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(m.enableSshUrl(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
" "$HOST_IP" "$HTTP_PORT" "$BEACON_PORT" "$RUNJAILED_PY")

echo "robot        : $ROBOT"
echo "host         : $HOST_IP  (http :$HTTP_PORT, beacon :$BEACON_PORT)"
echo "payload      : $PAYLOAD  [$(printf %s "$PAYLOAD" | wc -c) bytes]"

if [ "$DRY_RUN" = 1 ]; then
    exit 0
fi

# --- serve the scripts --------------------------------------------------------
LOG=$(mktemp -t roomba-oneshot-XXXXXX.log)
cleanup() {
    [ -n "${HTTP_PID:-}" ] && kill "$HTTP_PID" 2>/dev/null || true
    [ -n "${RAIL_PID:-}" ] && kill "$RAIL_PID" 2>/dev/null || true
    printf 'log: %s\n' "$LOG" "$LOG.http"
}
trap cleanup EXIT

if [ "$SERVE" = 1 ]; then
    ( cd "$SCRIPTS_DIR" && exec "$PY" -m http.server "$HTTP_PORT" --bind 0.0.0.0 ) >"$LOG.http" 2>&1 &
    HTTP_PID=$!
    sleep 1
    kill -0 "$HTTP_PID" 2>/dev/null || die "HTTP server on :$HTTP_PORT failed (port busy?)"
else
    echo "serving      : external (expecting $HOST_IP:$HTTP_PORT already up)"
    : >"$LOG.http"
fi

# --- publish ------------------------------------------------------------------
echo "publishing via ${ROBOT}:8883 ..."
RUNJAILED_BEACON_LOG="$LOG.beacons" "$PY" "$RUNJAILED_PY" -i "$ROBOT" -c "$HOST_IP" \
    --beacon-port "$BEACON_PORT" --linger "$LINGER" \
    --enable-ssh "$HOST_IP:$HTTP_PORT" >>"$LOG" 2>&1 &
RAIL_PID=$!

# wait until the payload is actually on the wire (or runjailed died)
for _ in $(seq 1 20); do
    if grep -q -e 'sendCmd rc=' -e 'sendCmd failed' "$LOG" 2>/dev/null; then
        break
    fi
    kill -0 "$RAIL_PID" 2>/dev/null || break
    sleep 0.5
done
sed -n '/Connected successfully/,$p' "$LOG" | sed 's/^/  /'
grep -q 'sendCmd rc=True' "$LOG" 2>/dev/null || {
    kill "$RAIL_PID" 2>/dev/null || true
    die "publish failed (log: $LOG)"
}

# --- wait for the robot -------------------------------------------------------
port_open() {
    timeout 3 bash -c "exec 3<>/dev/tcp/$ROBOT/$SSH_PORT" 2>/dev/null
}

show_beacons() {
    if [ -f "$LOG.beacons" ]; then
        sed 's/^/  beacon: /' "$LOG.beacons"
    fi
}

robot_ran_as_root() { grep -q '/whoami/' "$LOG.beacons" 2>/dev/null; }
ssh_enabled()       { grep -q '/done/ssh' "$LOG.beacons" 2>/dev/null; }

DEADLINE=$(( $(date +%s) + WAIT ))
sleep 3   # give the robot time to fetch and run the payload before probing
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if port_open; then
        sleep 1
        show_beacons
        echo "[OK] SSH is reachable on $ROBOT:$SSH_PORT"
        echo "     ssh -p $SSH_PORT root@$ROBOT"
        exit 0
    fi
    if grep -q 'Enabled SSH!' "$LOG" 2>/dev/null || ssh_enabled; then
        echo "[..] robot reports SSH enabled, waiting for the firewall ..."
    fi
    sleep 3
done

show_beacons
if robot_ran_as_root; then
    echo "[WARN] script ran as root, but TCP/$SSH_PORT stays closed."
    echo "       SYSTEM_ACCESS is written to the provisioning file; the firewall honours it"
    echo "       on the next start of the connectivity services. Reboot the robot and retry:"
    echo "         ./runjailed/oneclick-ssh.sh $ROBOT --ssh-port $SSH_PORT"
    exit 3
fi

echo "[FAIL] no beacon from the robot within ${WAIT}s."
echo "       check: robot reachable, BLID/password correct, payload <= 191 bytes,"
echo "       and that the robot can reach $HOST_IP:$HTTP_PORT (see $LOG)"
exit 1
