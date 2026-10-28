### roomba::unjailed
## Enables SSH on the robot. Runs as root (invoked through `busybox su -c` from
## the connectivity_manager sdiscUrl sink) and deploys the unit it needs itself.
##
##   enableSSH.sh <host> [port]
##
## Paths are overridable so the script can be dry-run outside the robot.

HOST=$1
PORT=${2:-8899}

S=${RUNJAILED_SCRIPTS:-/opt/irobot/persistent/opt/runjailed/scripts}
PROV=${RUNJAILED_PROVISIONING:-/opt/irobot/config/provisioning}
UNITS=${RUNJAILED_UNIT_DIR:-/lib/systemd/system}
SYSTEMCTL=${RUNJAILED_SYSTEMCTL:-systemctl}
MOUNT=${RUNJAILED_MOUNT:-mount}
CB=${RUNJAILED_CB:-http://$HOST:8883}

[ -n "$HOST" ] || { echo "usage: enableSSH.sh <host> [port]"; exit 2; }

# Beacon to runjailed.py on the host: prints which user executes the script.
wget -qO- "$CB/whoami/$(id -un)" >/dev/null 2>&1

mkdir -p "$S" || exit 1
cd "$S" || exit 1

for f in common.sh sshd.sh sshd.socket; do
  wget -qO"$f" "http://$HOST:$PORT/$f" || { echo "fetch $f failed"; exit 1; }
done
chmod a+x "$S"/*.sh 2>/dev/null

# Drop the strict firewall rules (takes effect once the networking stack re-reads
# the provisioning file, i.e. after a restart of the connectivity services).
if [ -f "$PROV" ]; then
  sed -i 's/SYSTEM_ACCESS.*/SYSTEM_ACCESS="unlocked"/' "$PROV"
else
  echo "WARNING: $PROV missing, firewall left locked"
fi

"$MOUNT" --bind "$S/sshd.socket" "$UNITS/sshd.socket" || { echo "mount --bind failed"; exit 1; }
"$SYSTEMCTL" enable --now sshd.socket || { echo "systemctl failed"; exit 1; }

echo "sshd.socket enabled on :22"
wget -qO- "$CB/done/ssh" >/dev/null 2>&1
