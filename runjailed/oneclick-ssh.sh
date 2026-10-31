#!/usr/bin/env bash
# One owner-controlled invocation: one HTTP listener, one MQTT publish, bounded
# evidence + SSH-banner verification. All lifecycle/status logic is in Python;
# never infer device success by grepping a local enqueue message.
# Usage: oneclick-ssh.sh ROBOT_IPV4 [--host-ip HOST] [--timeout SECONDS] [--dry-run]
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PY=${PYTHON:-}
if [ -z "$PY" ]; then
    for candidate in "$ROOT/.venv/bin/python" "$(command -v python3 || true)" "$(command -v python || true)"; do
        if [ -n "$candidate" ] && "$candidate" -c 'import paho.mqtt.client' 2>/dev/null; then
            PY=$candidate
            break
        fi
    done
fi
[ -n "$PY" ] || { echo 'No Python with paho-mqtt; export PYTHON=/path/to/venv/bin/python' >&2; exit 1; }
if [ "$#" -eq 0 ]; then
    echo 'usage: oneclick-ssh.sh ROBOT_IPV4 [options]; use --help for details' >&2
    exit 2
fi
if [ "$1" = --help ] || [ "$1" = -h ]; then
    exec "$PY" "$ROOT/runjailed/runjailed.py" --help
fi
ROBOT=$1
shift
exec "$PY" "$ROOT/runjailed/runjailed.py" --ip "$ROBOT" --enable-ssh "$@"
