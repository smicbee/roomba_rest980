"""Host-side loopback tests only; no robot, firmware or external network evidence."""
import contextlib
import http.client
import threading
import socket
import time
import subprocess
import os
import sys
from unittest.mock import Mock

import pytest

from test_runjailed import SCRIPT, load_script


@contextlib.contextmanager
def local_http(module):
    server = module.HTTPServer(("127.0.0.1", 0), module.RunjailedHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def get(server, path):
    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("filename", ["enableSSH.sh", "sshd.socket"])
def test_same_listener_serves_requested_robot_artifact(filename):
    module = load_script()
    with local_http(module) as server:
        status, body = get(server, "/" + filename)
    assert status == 200
    assert body == (SCRIPT.parent / "scripts" / filename).read_bytes()
    assert body


@pytest.mark.parametrize("path", ["/", "/../runjailed.py", "/%2e%2e/runjailed.py",
                                  "/common.sh", "/whoami/rooted", "/done/ssh?fake=1"])
def test_http_unknown_or_traversal_path_is_not_success(path):
    with local_http(load_script()) as server:
        status, _ = get(server, path)
    assert status == 404


def test_http_evidence_counts_only_this_attempt_from_expected_robot():
    module = load_script()
    with local_http(module) as server:
        evidence = module.DeviceEvidence("127.0.0.1")
        server.evidence = evidence
        get(server, "/enableSSH.sh")  # Before publish: not this attempt.
        assert "/enableSSH.sh" in evidence.missing()
        evidence.arm()
        for path in ("/whoami/apps", "/whoami/rooted", "/done/ssh?fake=1"):
            get(server, path)
        assert set(evidence.missing()) == set(evidence.REQUIRED)
        for path in evidence.REQUIRED:
            assert get(server, path)[0] == 200
        deadline = time.monotonic() + 1
        while evidence.missing() and time.monotonic() < deadline:
            time.sleep(0.005)  # Response receipt can race server-side recording.
        assert evidence.missing() == []


def test_http_foreign_source_is_rejected_not_counted_as_device():
    module = load_script()
    with local_http(module) as server:
        server.evidence = module.DeviceEvidence("127.0.0.2")
        server.evidence.arm()
        for path in server.evidence.REQUIRED:
            assert get(server, path)[0] == 403
        assert set(server.evidence.missing()) == set(server.evidence.REQUIRED)


@contextlib.contextmanager
def banner_server(data=b"SSH-2.0-OpenSSH_8.2\r\n", silent=False):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    stop = threading.Event()
    errors = []

    def run():
        try:
            conn, _ = listener.accept()
            with conn:
                if silent:
                    stop.wait(1)
                else:
                    conn.sendall(data)
        except OSError as exc:
            if not stop.is_set():
                errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        listener.close()
        thread.join(3)
        assert not thread.is_alive()
        assert not errors


def test_banner_probe_reads_real_loopback_identification_not_merely_open_tcp():
    module = load_script()
    with banner_server() as port:
        assert module.read_ssh_banner("127.0.0.1", port, 0.2) == "SSH-2.0-OpenSSH_8.2"


@pytest.mark.parametrize("data", [b"HTTP/1.0 200 OK\r\n", b"SSH-2.0-\r\n",
                                  b"SSH-2.0-partial", b"SSH-2.0-" + b"x" * 256 + b"\r\n"])
def test_open_nonssh_or_incomplete_banner_is_not_success(data):
    with banner_server(data) as port:
        assert load_script().read_ssh_banner("127.0.0.1", port, 0.1) is None


def test_silent_open_port_banner_probe_is_bounded():
    start = time.monotonic()
    with banner_server(silent=True) as port:
        assert load_script().read_ssh_banner("127.0.0.1", port, 0.05) is None
    assert time.monotonic() - start < 0.5


def test_device_verification_requires_downloads_root_done_and_real_banner():
    module = load_script()
    with local_http(module) as server, banner_server() as port:
        evidence = module.DeviceEvidence("127.0.0.1")
        evidence.arm()
        server.evidence = evidence
        for path in evidence.REQUIRED:
            get(server, path)
        assert module.verify_device(evidence, "127.0.0.1", port, 0.3,
                                    module.ConnectionState()) == "SSH-2.0-OpenSSH_8.2"


@pytest.mark.parametrize("missing", ["/enableSSH.sh", "/sshd.socket", "/whoami/root", "/done/ssh"])
def test_partial_evidence_cannot_report_success_even_with_existing_ssh(missing, monkeypatch, capsys):
    module = load_script()
    with local_http(module) as server:
        evidence = module.DeviceEvidence("127.0.0.1")
        evidence.arm()
        server.evidence = evidence
        for path in evidence.REQUIRED:
            if path != missing:
                get(server, path)
        probe = Mock()
        monkeypatch.setattr(module, "read_ssh_banner", probe)
        assert module.verify_device(evidence, "127.0.0.1", 22, 0.02,
                                    module.ConnectionState()) is None
        probe.assert_not_called()
    assert missing in capsys.readouterr().err


def test_callback_route_uses_robot_not_an_unrelated_internet_host(monkeypatch):
    module = load_script()
    sock = Mock()
    sock.getsockname.return_value = ("192.0.2.2", 12345)
    monkeypatch.setattr(module.socket, "socket", Mock(return_value=sock))
    assert module.get_local_ip("192.0.2.1") == "192.0.2.2"
    sock.connect.assert_called_once_with(("192.0.2.1", 8883))
    sock.close.assert_called_once()


def test_callback_hostname_is_resolved_once_to_validated_ipv4(monkeypatch):
    module = load_script()
    dns = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 0))])
    monkeypatch.setattr(module.socket, "getaddrinfo", dns)
    assert module.resolve_callback("host.example") == "192.0.2.2"
    dns.assert_called_once()


@pytest.mark.parametrize("host", ["host;id", "$(id)", "a/b", "host\n", "1.2.3.999"])
def test_invalid_host_is_rejected_before_dns(host, monkeypatch):
    module = load_script()
    dns = Mock()
    monkeypatch.setattr(module.socket, "getaddrinfo", dns)
    with pytest.raises(ValueError):
        module.resolve_callback(host)
    dns.assert_not_called()


def test_ambiguous_callback_dns_is_rejected_instead_of_broad_firewall(monkeypatch):
    module = load_script()
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ("192.0.2.2", "192.0.2.3")])
    with pytest.raises(ValueError, match="one IPv4"):
        module.resolve_callback("host.example")


def test_dns_resolution_wait_is_bounded(monkeypatch):
    module = load_script()
    release = threading.Event()
    monkeypatch.setattr(module, "RESOLVE_TIMEOUT", 0.01, raising=False)
    def slow_dns(*args, **kwargs):
        release.wait(1)
        return []
    monkeypatch.setattr(module.socket, "getaddrinfo", slow_dns)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="resolution"):
            module.resolve_callback("host.example")
        assert time.monotonic() - start < 0.5
    finally:
        release.set()


def test_oneclick_real_subprocess_dry_run_without_credentials_or_network():
    env = {k: v for k, v in os.environ.items() if k not in ("ROOMBA_BLID", "ROOMBA_PASSWORD")}
    env["PYTHON"] = sys.executable
    result = subprocess.run(["bash", str(SCRIPT.parent / "oneclick-ssh.sh"),
        "192.0.2.1", "--host-ip", "192.0.2.2", "--dry-run"],
        env=env, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "/usr/bin/curl" in result.stdout and "/bin/busybox.suid su -c" in result.stdout
    assert "wifictl" in result.stdout and ":8883/enableSSH.sh" in result.stdout


@pytest.mark.parametrize("options", [["--timeout"], ["--timeout", "nan"], ["--no-serve"],
    ["--http-port", "8883", "--beacon-port", "8899"]])
def test_oneclick_real_cli_bad_options_exit_usage_without_publish(options):
    env = {k: v for k, v in os.environ.items() if k not in ("ROOMBA_BLID", "ROOMBA_PASSWORD")}
    env["PYTHON"] = sys.executable
    result = subprocess.run(["bash", str(SCRIPT.parent / "oneclick-ssh.sh"),
        "192.0.2.1", "--host-ip", "192.0.2.2", "--dry-run", *options],
        env=env, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    assert result.returncode == 2, result.stderr
    assert "[VERIFIED]" not in result.stdout


def test_requests_started_before_publish_cannot_be_counted_after_arm():
    module = load_script()
    evidence = module.DeviceEvidence("127.0.0.1")
    old = evidence.ticket()
    evidence.arm()
    for path in evidence.REQUIRED:
        evidence.record(path, "127.0.0.1", old)
    assert set(evidence.missing()) == set(evidence.REQUIRED)
    current = evidence.ticket()
    for path in evidence.REQUIRED:
        evidence.record(path, "127.0.0.1", current)
    assert not evidence.missing()
    evidence.arm()
    evidence.record("/done/ssh", "127.0.0.1", current)
    assert "/done/ssh" in evidence.missing()


def test_http_cleanup_is_bounded_even_when_a_client_stalls_request(monkeypatch):
    module = load_script()
    accepted = threading.Event()
    setup = module.RunjailedHTTPHandler.setup
    def observed_setup(self):
        setup(self)
        accepted.set()
    monkeypatch.setattr(module.RunjailedHTTPHandler, "setup", observed_setup)
    peer = None
    try:
        with local_http(module) as server:
            peer = socket.create_connection(server.server_address, timeout=1)
            peer.sendall(b"GET /enableSSH.sh")  # No completed request line.
            assert accepted.wait(1)
            start = time.monotonic()
        assert time.monotonic() - start < 0.5
    finally:
        if peer:
            peer.close()
