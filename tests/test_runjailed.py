"""Offline regression tests; never start servers or contact a robot/broker."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import paho.mqtt.client as mqtt
import pytest


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
def test_send_cmd_publishes_discovery_url_as_json_with_qos_one(url):
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)

    assert module.sendCmd(url) is True

    module.mqttc.publish.assert_called_once_with(
        "wifictl", json.dumps({"state": {"sdiscUrl": url}}), qos=1
    )


def test_send_cmd_preserves_shell_metacharacters_inside_the_url_field():
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)
    value = 'https://example.invalid/$(id); echo injected'

    assert module.sendCmd(value) is True

    module.mqttc.publish.assert_called_once_with(
        "wifictl", json.dumps({"state": {"sdiscUrl": value}}), qos=1
    )


def test_send_cmd_preserves_json_special_characters_within_firmware_limit():
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)
    url = 'https://example.org/discovery/' + 'x' * 136 + '?label="robot"&path=\\test'

    assert len(url.encode()) <= 191
    assert module.sendCmd(url) is True

    args, kwargs = module.mqttc.publish.call_args
    assert args[0] == "wifictl"
    assert json.loads(args[1]) == {"state": {"sdiscUrl": url}}
    assert kwargs == {"qos": 1}


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


def test_enable_ssh_url_stays_within_firmware_limit_and_uses_root_wrapper():
    module = load_script()

    url = module.enableSshUrl("192.168.178.42")

    assert len(url.encode()) <= 191
    assert url.startswith("https://$(/bin/busybox su -c '")
    assert url.endswith("')")
    assert "http://192.168.178.42:8899/enableSSH.sh" in url


def test_enable_ssh_url_rejects_overlong_hosts_instead_of_truncating():
    module = load_script()

    with pytest.raises(ValueError, match="191"):
        module.enableSshUrl("host-" + "x" * 200)


def test_on_connect_publishes_pending_urls_once_and_disconnects():
    module = load_script()
    module.mqttc = Mock(spec=mqtt.Client)
    module.mqttc.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)
    module.PENDING_URLS[:] = ["https://example.org/a", "https://example.org/b"]
    module.SEND_DELAY = 0
    client = Mock()

    with patch("threading.Thread") as thread:
        thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"](
            *thread.call_args.kwargs["args"]
        )
        module.on_connect(client, None, None, 0, None)

    assert module.mqttc.publish.call_count == 2
    assert module.PENDING_URLS == []
    client.disconnect.assert_called_once()
