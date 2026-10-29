"""Offline regression tests; only in-process sockets, never contact a robot/broker."""
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion

SCRIPT = Path(__file__).resolve().parents[1] / "runjailed" / "runjailed.py"


def load_script():
    spec = importlib.util.spec_from_file_location("runjailed_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_does_not_parse_arguments_or_start_network_services():
    with (
        patch("sys.argv", ["unrelated-program"]),
        patch("argparse.ArgumentParser.parse_args") as parse_args,
        patch("socket.socket") as socket,
        patch("http.server.HTTPServer") as server,
        patch("threading.Thread") as thread,
        patch("paho.mqtt.client.Client") as client,
    ):
        load_script()
    parse_args.assert_not_called()
    socket.assert_not_called()
    server.assert_not_called()
    thread.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize("url", ["http://example.org", "https://example.org"])
def test_send_cmd_publishes_discovery_url_as_json_with_qos_zero_without_retain(url):
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)

    assert module.sendCmd(url) is True

    module.mqttc.publish.assert_called_once_with(
        "delta", json.dumps({"state": {"sdiscUrl": url}}), qos=0, retain=False
    )


def test_send_cmd_preserves_shell_metacharacters_inside_the_url_field():
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)
    value = 'https://example.invalid/$(id); echo injected'

    assert module.sendCmd(value) is True

    module.mqttc.publish.assert_called_once_with(
        "delta", json.dumps({"state": {"sdiscUrl": value}}), qos=0, retain=False
    )


def test_send_cmd_preserves_json_special_characters_within_firmware_limit():
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)
    url = 'https://example.org/discovery/' + 'x' * 136 + '?label="robot"&path=\\test'

    assert len(url.encode()) <= 191
    assert module.sendCmd(url) is True

    args, kwargs = module.mqttc.publish.call_args
    assert args[0] == "delta"
    assert json.loads(args[1]) == {"state": {"sdiscUrl": url}}
    assert kwargs == {"qos": 0, "retain": False}


@pytest.mark.parametrize("value", [None, 123, "x" * 192])
def test_send_cmd_rejects_invalid_values_without_publishing(value):
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)

    with pytest.raises(ValueError, match="191 bytes"):
        module.sendCmd(value)

    module.mqttc.publish.assert_not_called()


@pytest.mark.parametrize("rc", [mqtt.MQTT_ERR_NO_CONN, mqtt.MQTT_ERR_QUEUE_SIZE])
def test_send_cmd_reports_publish_enqueue_failure(rc):
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=rc)

    assert module.sendCmd("https://example.org/discovery") is False
    module.mqttc.publish.assert_called_once()


def test_send_cmd_requires_an_initialized_mqtt_client():
    module = load_script()
    with pytest.raises(RuntimeError, match="MQTT client"):
        module.sendCmd("https://example.org/discovery")


def test_shadow_update_only_reports_discovery_without_running_shell_commands(capsys):
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    message = SimpleNamespace(
        topic="$aws/things/test/shadow/update",
        payload=json.dumps({"state": {"reported": {"sku": "test", "softwareVer": "test"}}}).encode(),
    )

    module.on_message(module.mqttc, None, message)

    module.mqttc.publish.assert_not_called()
    output = capsys.readouterr().out
    assert "Discovered: test on test" in output
    assert "disabled" in output


@pytest.mark.parametrize("helper_name", ["sendChunkedCommand", "runAsRoot"])
@pytest.mark.parametrize("value", ["echo hello", "https://example.org/discovery"])
def test_legacy_shell_helpers_fail_explicitly_without_publishing(helper_name, value):
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)

    with pytest.raises(NotImplementedError, match="discovery URL"):
        getattr(module, helper_name)(value)

    module.mqttc.publish.assert_not_called()


def test_enable_ssh_url_stays_within_firmware_limit_and_avoids_su_by_default():
    module = load_script()

    url = module.enableSshUrl("192.168.178.42")

    assert len(url.encode()) <= 191
    assert " su " not in url
    assert url.startswith("https://$(wget -qO/tmp/e ")
    assert "http://192.168.178.42:8899/enableSSH.sh" in url


def test_enable_ssh_url_can_opt_into_the_setuid_root_wrapper():
    module = load_script()

    url = module.enableSshUrl("192.168.178.42", use_su=True)

    assert url.startswith("https://$(/bin/busybox su -c '")
    assert len(url.encode()) <= 191


def test_enable_ssh_url_rejects_overlong_hosts_instead_of_truncating():
    module = load_script()

    with pytest.raises(ValueError, match="191"):
        module.enableSshUrl("host-" + "x" * 200)


@pytest.mark.parametrize("reason", [0, 135])
def test_on_connect_only_signals_result_never_starts_a_publish_worker(reason):
    module = load_script()
    state = module.ConnectionState()
    client = Mock()

    with patch("threading.Thread") as thread:
        module.on_connect(client, state, None, reason, None)

    assert state.connack.is_set()
    assert state.accepted is (reason == 0)
    client.publish.assert_not_called()
    thread.assert_not_called()
    if reason == 0:
        client.subscribe.assert_called_once_with("$SYS/#")
    else:
        client.subscribe.assert_not_called()


def test_on_message_ignores_non_json_payloads_and_other_topics():
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)

    module.on_message(module.mqttc, None, SimpleNamespace(topic="$SYS/broker/uptime", payload=b"120 seconds"))
    module.on_message(module.mqttc, None, SimpleNamespace(topic="$aws/things/x/shadow/update", payload=b"not json"))

    module.mqttc.publish.assert_not_called()


def test_enable_ssh_url_passes_beacon_port_to_the_remote_script():
    module = load_script()

    url = module.enableSshUrl("10.0.0.5", 8899, 8887)

    assert "sh /tmp/e 10.0.0.5 8899 8887" in url
    assert len(url.encode()) <= 191


@pytest.fixture
def cli(monkeypatch):
    module = load_script()
    client = Mock(spec=mqtt.Client)
    info = Mock(spec=mqtt.MQTTMessageInfo)
    info.rc = mqtt.MQTT_ERR_SUCCESS
    info.is_published.return_value = True
    client.publish.return_value = info
    client.connect.return_value = mqtt.MQTT_ERR_SUCCESS
    client.loop_start.return_value = mqtt.MQTT_ERR_SUCCESS
    client.socket.return_value = None
    factory = Mock(return_value=client)
    monkeypatch.setattr(module.mqtt, "Client", factory)
    server = Mock()
    monkeypatch.setattr(module, "HTTPServer", Mock(return_value=server))
    monkeypatch.setattr(module.threading, "Thread", Mock())
    monkeypatch.setattr(module, "CONNECT_TIMEOUT", 0.01, raising=False)
    monkeypatch.setattr(module, "PUBLISH_TIMEOUT", 0.01, raising=False)
    argv = ["runjailed", "-i", "192.0.2.1", "-b", "test", "-p", "test",
            "-c", "192.0.2.2", "--send-delay", "0", "--linger", "0",
            "--send", "https://example.org/a", "--send", "https://example.org/b"]
    monkeypatch.setattr("sys.argv", argv)

    def start():
        state = factory.call_args.kwargs["userdata"]
        client.on_connect(client, state, {}, 0, None)
        return mqtt.MQTT_ERR_SUCCESS

    client.loop_start.side_effect = start
    return SimpleNamespace(module=module, client=client, info=info, factory=factory,
                           server=server, argv=argv)


def test_main_owns_publish_and_waits_for_local_send_before_cleanup(cli):
    assert cli.module.main() == 0

    assert cli.factory.call_args.kwargs["reconnect_on_failure"] is False
    cli.client.loop_start.assert_called_once()
    cli.client.loop_forever.assert_not_called()
    assert cli.client.publish.call_count == 2
    assert cli.info.wait_for_publish.call_args_list == [
        call(timeout=0.01), call(timeout=0.01)]
    assert cli.info.is_published.call_count == 2
    cli.client.disconnect.assert_called_once()
    cli.client.loop_stop.assert_called_once()
    cli.server.shutdown.assert_called_once()
    cli.server.server_close.assert_called_once()


@pytest.mark.parametrize("option", ["--send-delay", "--linger"])
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "-inf"])
def test_cli_rejects_nonfinite_or_negative_delays_before_starting_services(cli, option, value, capsys):
    index = cli.argv.index(option)
    cli.argv[index:index + 2] = [f"{option}={value}"]
    with pytest.raises(SystemExit) as exc:
        cli.module.main()
    assert exc.value.code == 2
    assert "must be finite and nonnegative" in capsys.readouterr().err
    cli.module.HTTPServer.assert_not_called()
    cli.factory.assert_not_called()


@pytest.mark.parametrize("phase", ["before_connack", "before_publish", "during_send", "between_publishes", "linger"])
def test_main_fails_on_disconnect_without_continuing_or_retrying(cli, phase):
    start = cli.client.loop_start.side_effect

    def disconnect():
        cli.client.on_disconnect(cli.client, cli.factory.call_args.kwargs["userdata"],
                                 {}, mqtt.MQTT_ERR_CONN_LOST, None)

    if phase in ("before_connack", "before_publish"):
        def start_then_disconnect():
            if phase == "before_publish":
                start()
            disconnect()
            return mqtt.MQTT_ERR_SUCCESS
        cli.client.loop_start.side_effect = start_then_disconnect
    elif phase == "during_send":
        cli.info.wait_for_publish.side_effect = lambda **kwargs: disconnect()
    else:
        # Event.wait is the interruptible delay: deliver a disconnect as it starts.
        state_type = cli.module.ConnectionState
        def state_factory():
            state = state_type()
            original_wait = state.disconnected.wait
            def wait(timeout=None):
                disconnect()
                return original_wait(0)
            state.disconnected.wait = wait
            return state
        cli.module.ConnectionState = state_factory
        if phase == "linger":
            del cli.argv[-2:]
            cli.argv[cli.argv.index("--linger") + 1] = "1"

    assert cli.module.main() == 1
    assert cli.client.publish.call_count <= (0 if phase.startswith("before_") else 1)
    cli.client.connect.assert_called_once()
    cli.client.loop_stop.assert_called_once()
    cli.client.disconnect.assert_called_once()


@pytest.mark.parametrize("failure", ["auth", "connack_timeout", "socket", "connect_rc", "loop_start"])
def test_main_connection_failures_never_publish_or_retry(cli, failure, capsys):
    if failure == "auth":
        def rejected():
            cli.client.on_connect(cli.client, cli.factory.call_args.kwargs["userdata"],
                                  {}, 135, None)
            return mqtt.MQTT_ERR_SUCCESS
        cli.client.loop_start.side_effect = rejected
    elif failure == "connack_timeout":
        cli.client.loop_start.side_effect = None
    elif failure == "socket":
        cli.client.connect.side_effect = TimeoutError("test socket timeout")
    elif failure == "connect_rc":
        cli.client.connect.return_value = mqtt.MQTT_ERR_NO_CONN
    else:
        cli.client.loop_start.side_effect = None
        cli.client.loop_start.return_value = mqtt.MQTT_ERR_INVAL

    assert cli.module.main() == 1
    cli.client.publish.assert_not_called()
    cli.client.connect.assert_called_once()
    cli.client.disconnect.assert_called_once()
    assert cli.client.loop_stop.call_count == (failure in ("auth", "connack_timeout"))
    cli.server.server_close.assert_called_once()
    output = capsys.readouterr()
    assert "MQTT failed:" in output.err
    assert "Connected successfully!" not in output.out


@pytest.mark.parametrize("failure", ["no_conn", "queue_full", "incomplete", "wait_error"])
def test_main_send_failures_stop_at_first_url_and_cleanup(cli, failure):
    if failure == "no_conn":
        cli.info.rc = mqtt.MQTT_ERR_NO_CONN
    elif failure == "queue_full":
        cli.info.rc = mqtt.MQTT_ERR_QUEUE_SIZE
    elif failure == "incomplete":
        cli.info.is_published.return_value = False
    else:
        cli.info.wait_for_publish.side_effect = RuntimeError("test send failed")

    assert cli.module.main() == 1
    cli.client.publish.assert_called_once()
    cli.client.disconnect.assert_called_once()
    cli.client.loop_stop.assert_called_once()
    cli.server.server_close.assert_called_once()


def test_main_legacy_credentials_from_environment_and_enable_ssh_option(cli, monkeypatch):
    del cli.argv[-4:]
    del cli.argv[cli.argv.index("-b"):cli.argv.index("-b") + 4]
    monkeypatch.setenv("ROOMBA_BLID", "env-blid")
    monkeypatch.setenv("ROOMBA_PASSWORD", "env-password")
    monkeypatch.setenv("ROOMBA_BUSYBOX", "/bin/busybox")
    cli.argv.extend(["--enable-ssh", "192.0.2.2:8899", "--beacon-port", "8887"])

    assert cli.module.main() == 0
    cli.client.username_pw_set.assert_called_once_with("env-blid", "env-password")
    url = cli.module.enableSshUrl("192.0.2.2", 8899, 8887)
    cli.client.publish.assert_called_once_with(
        "delta", json.dumps({"state": {"sdiscUrl": url}}), qos=0, retain=False)


def test_cleanup_unblocks_socket_before_joining_a_stalled_network_loop(cli, monkeypatch):
    sock = Mock()
    cli.client.socket.return_value = sock
    monkeypatch.setattr(cli.module, "CLEANUP_TIMEOUT", 0.01, raising=False)
    def stop():
        sock.shutdown.assert_called_once_with(cli.module.socket.SHUT_RDWR)
    cli.client.loop_stop.side_effect = stop

    assert cli.module.main() == 0
    cli.client.disconnect.assert_called_once()
    cli.client.loop_stop.assert_called_once()


# These tests use the real installed Paho serializer and network loop. Only the
# transport is replaced by a socketpair; no TCP listener, TLS or robot is used.
def read_mqtt_packet(peer):
    def read_exact(count):
        data = bytearray()
        while len(data) < count:
            chunk = peer.recv(count - len(data))
            if not chunk:
                raise EOFError("MQTT peer closed")
            data.extend(chunk)
        return bytes(data)

    header = read_exact(1)
    encoded_length = bytearray()
    length = 0
    for shift in range(0, 28, 7):
        byte = read_exact(1)[0]
        encoded_length.append(byte)
        length += (byte & 127) << shift
        if not byte & 128:
            body = read_exact(length)
            return header + bytes(encoded_length) + body, body
    raise AssertionError("malformed MQTT remaining length")


@pytest.fixture
def real_mqtt_cli(monkeypatch):
    module = load_script()
    client = mqtt.Client(CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
    local, peer = socket.socketpair()
    peer.settimeout(2)
    monkeypatch.setattr(client, "_create_socket", lambda: local)
    monkeypatch.setattr(client, "tls_set", Mock())
    monkeypatch.setattr(client, "tls_insecure_set", Mock())
    def factory(*args, **kwargs):
        assert args == (CallbackAPIVersion.VERSION2,)
        assert set(kwargs) == {"userdata", "reconnect_on_failure"}
        assert kwargs["reconnect_on_failure"] is False
        client.user_data_set(kwargs["userdata"])
        return client
    monkeypatch.setattr(module.mqtt, "Client", factory)
    monkeypatch.setattr(module, "HTTPServer", Mock(return_value=Mock()))
    monkeypatch.setattr(module, "CONNECT_TIMEOUT", 0.2)
    monkeypatch.setattr(module, "PUBLISH_TIMEOUT", 0.2)
    monkeypatch.setattr(module, "CLEANUP_TIMEOUT", 0.05)
    argv = ["runjailed", "-i", "192.0.2.1", "-b", "test", "-p", "test",
            "-c", "192.0.2.2", "--send-delay", "0", "--linger", "0",
            "--send", "https://example.org/a", "--send", "https://example.org/b"]
    monkeypatch.setattr("sys.argv", argv)
    harness = SimpleNamespace(module=module, client=client, peer=peer, argv=argv,
                              packets=[], errors=[])

    def broker_thread(action):
        def run():
            try:
                packet, _ = read_mqtt_packet(peer)
                assert packet[0] == 0x10  # CONNECT
                harness.packets.append(packet)
                action(harness)
            except (AssertionError, OSError, EOFError) as exc:
                harness.errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread
    harness.broker_thread = broker_thread
    yield harness
    # Avoid leaking threads/sockets even if an assertion fails.
    try:
        local.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    client.loop_stop()
    local.close()
    peer.close()


def test_real_paho_forced_partial_subscribe_write_cannot_interleave_publishes(real_mqtt_cli, monkeypatch):
    h = real_mqtt_cli
    partial_subscribe = threading.Event()
    publish_queued = threading.Event()
    network_writers = []
    publisher_threads = []
    send = h.client._sock_send
    publish = h.client.publish

    def partial_send(data):
        count = send(data[:3])  # Force real short writes, including PUBLISH frames.
        if data[0] == 0x82 and not partial_subscribe.is_set():
            # _packet_write has popped SUBSCRIBE, but not requeued its unsent
            # suffix. Enqueue PUBLISH at exactly the old two-writer race window.
            partial_subscribe.set()
            assert publish_queued.wait(2), "main thread did not enqueue PUBLISH"
        if partial_subscribe.is_set():
            network_writers.append(threading.current_thread())
        return count

    def queued_publish(*args, **kwargs):
        assert partial_subscribe.wait(2), "SUBSCRIBE was not partially written"
        publisher_threads.append(threading.current_thread())
        info = publish(*args, **kwargs)
        publish_queued.set()
        return info

    monkeypatch.setattr(h.client, "_sock_send", partial_send)
    monkeypatch.setattr(h.client, "publish", queued_publish)

    def broker(h):
        h.peer.sendall(b"\x20\x02\x00\x00")  # successful CONNACK
        while True:
            packet, _ = read_mqtt_packet(h.peer)
            h.packets.append(packet)
            if packet[0] == 0xE0:
                break
    thread = h.broker_thread(broker)
    caller = threading.current_thread()
    assert h.module.main() == 0
    thread.join(2)
    assert not thread.is_alive()
    assert h.errors == []
    assert partial_subscribe.is_set() and publish_queued.is_set()
    assert publisher_threads == [caller, caller]
    assert network_writers and all(writer is not caller for writer in network_writers)
    assert len(set(network_writers)) == 1
    assert [packet[0] for packet in h.packets] == [0x10, 0x82, 0x30, 0x30, 0xE0]
    assert h.packets[1] == b"\x82\x0b\x00\x01\x00\x06$SYS/#\x00"
    for packet, url in zip(h.packets[2:4], ["https://example.org/a", "https://example.org/b"]):
        payload = json.dumps({"state": {"sdiscUrl": url}}).encode()
        body = b"\x00\x05delta" + payload
        assert packet == b"\x30" + bytes([len(body)]) + body
    assert h.packets[-1] == b"\xe0\x00"
    assert h.client._thread is None


@pytest.mark.parametrize("failure", ["auth", "connack_timeout", "disconnect_before_connack", "send_timeout"])
def test_real_paho_failure_paths_are_bounded_and_never_retry(real_mqtt_cli, monkeypatch, failure, capsys):
    h = real_mqtt_cli
    connect = Mock(wraps=h.client.connect)
    publish = Mock(wraps=h.client.publish)
    monkeypatch.setattr(h.client, "connect", connect)
    monkeypatch.setattr(h.client, "publish", publish)
    if failure == "send_timeout":
        send = h.client._sock_send
        def stalled_send(data):
            if data[0] == 0x30:
                raise BlockingIOError("forced unwritable PUBLISH")
            return send(data)
        monkeypatch.setattr(h.client, "_sock_send", stalled_send)

    def broker(h):
        if failure == "disconnect_before_connack":
            h.peer.shutdown(socket.SHUT_RDWR)
            return
        if failure == "auth":
            h.peer.sendall(b"\x20\x02\x00\x05")  # MQTT 3.1.1 not authorized
        elif failure == "send_timeout":
            h.peer.sendall(b"\x20\x02\x00\x00")
        while True:
            try:
                packet, _ = read_mqtt_packet(h.peer)
            except EOFError:
                return
            h.packets.append(packet)
            if packet[0] == 0xE0:
                return
    thread = h.broker_thread(broker)
    start = time.monotonic()
    assert h.module.main() == 1
    elapsed = time.monotonic() - start
    thread.join(2)
    assert not thread.is_alive()
    assert h.errors == []
    assert elapsed < 1.5, f"failure/cleanup took {elapsed:.3f}s"
    connect.assert_called_once()
    assert publish.call_count == (1 if failure == "send_timeout" else 0)
    assert h.client._thread is None
    assert h.client.socket() is None
    assert not any(packet[0] == 0x30 for packet in h.packets)
    output = capsys.readouterr()
    assert "MQTT failed:" in output.err
    if failure != "send_timeout":
        assert "Connected successfully!" not in output.out


def test_cli_without_send_preserves_passive_monitoring_until_disconnect(cli):
    del cli.argv[-4:]
    state_type = cli.module.ConnectionState
    waits = []

    def state_factory():
        state = state_type()

        def wait(timeout=None):
            waits.append(timeout)
            cli.client.on_disconnect(cli.client, state, {}, mqtt.MQTT_ERR_CONN_LOST, None)
            return True

        state.disconnected.wait = wait
        return state

    cli.module.ConnectionState = state_factory
    assert cli.module.main() == 1
    assert waits == [None]  # Monitor-only mode runs until the connection closes.
    cli.client.publish.assert_not_called()
