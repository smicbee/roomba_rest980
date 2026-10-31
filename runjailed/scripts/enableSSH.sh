#!/bin/sh
# Robot-only bootstrap for the owner's curl/busybox.suid workflow.
# Runtime changes only: no provisioning, firmware, persistent unit or cloud edits.
# enableSSH.sh <resolved-callback-ipv4> [shared-http-port]
set -eu

fail() { echo "enableSSH.sh: $*" >&2; exit 1; }
usage() { echo "usage: enableSSH.sh <unicast IPv4> [port 1..65535]" >&2; exit 2; }

HOST=${1:-}
PORT=${2:-8883}
[ "$#" -le 2 ] || usage
case "$HOST" in ''|*[!0-9.]*|0.0.0.0|255.255.255.255) usage ;; esac
# Split only after excluding every shell metacharacter, whitespace and glob.
OLD_IFS=$IFS
IFS=.
set -- $HOST
IFS=$OLD_IFS
[ "$#" -eq 4 ] || usage
for octet do
    case "$octet" in ''|0[0-9]*) usage ;; esac
    [ "${#octet}" -le 3 ] && [ "$octet" -le 255 ] || usage
done
[ "$1" -lt 224 ] || usage
case "$PORT" in ''|*[!0-9]*|0[0-9]*) usage ;; esac
[ "${#PORT}" -le 5 ] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || usage
[ "$(id -u)" = 0 ] || fail "requires root via /bin/busybox.suid su -c"

# Overrides are for isolated command-recording tests, never evidence of firmware.
CURL=${RUNJAILED_CURL:-/usr/bin/curl}
UNITS=${RUNJAILED_UNIT_DIR:-/run/systemd/system}
SYSTEMCTL=${RUNJAILED_SYSTEMCTL:-systemctl}
IPTABLES=${RUNJAILED_IPTABLES:-iptables}
BASE=http://$HOST:$PORT
fetch() { "$CURL" -fSs --connect-timeout 5 --max-time 15 -o "$1" "$BASE/$2"; }

fetch /dev/null whoami/root || fail "root beacon failed"
UNIT=$(mktemp "${TMPDIR:-/tmp}/runjailed-sshd.XXXXXX") || fail "mktemp failed"
trap 'rm -f "$UNIT"' EXIT
trap 'exit 1' HUP INT TERM
fetch "$UNIT" sshd.socket || fail "fetch sshd.socket failed"
[ -s "$UNIT" ] || fail "empty sshd.socket"
mkdir -p "$UNITS" || fail "runtime unit directory failed"
cp "$UNIT" "$UNITS/sshd.socket" || fail "runtime unit copy failed"

# Never flush, delete or broaden existing rules. Repeated explicit invocation
# checks for these exact rules, rather than inserting duplicates.
"$IPTABLES" -C INPUT -p tcp -s "$HOST" --dport 22 -j ACCEPT 2>/dev/null ||
    "$IPTABLES" -I INPUT 1 -p tcp -s "$HOST" --dport 22 -j ACCEPT || fail "SSH INPUT rule failed"
"$IPTABLES" -C OUTPUT -p tcp -d "$HOST" --sport 22 -j ACCEPT 2>/dev/null ||
    "$IPTABLES" -I OUTPUT 1 -p tcp -d "$HOST" --sport 22 -j ACCEPT || fail "SSH OUTPUT rule failed"
"$SYSTEMCTL" daemon-reload || fail "daemon-reload failed"
"$SYSTEMCTL" start sshd.socket || fail "start sshd.socket failed"
fetch /dev/null done/ssh || fail "completion beacon failed"
echo "sshd.socket started (runtime only); host must still verify the SSH banner"
