"""Isolated command-recording shell fakes; NOT firmware or hardware evidence.

All service/firewall/network commands are replaced. All writable paths are under
pytest's temporary directory. Never run the real robot script on the host alone.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_runjailed import SCRIPT

ROBOT_SCRIPT = SCRIPT.parent / "scripts" / "enableSSH.sh"


@pytest.fixture
def robot_shell(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    recorder = fakebin / "recorder"
    recorder.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['RECORD'], 'a') as f:
    f.write(json.dumps([name, *args]) + '\n')
if name == 'id':
    print(os.environ.get('FAKE_UID', '0') if args == ['-u'] else 'root')
    sys.exit(0)
if name in ('curl', 'wget'):
    if os.environ.get('FAIL_FETCH') and args[-1].endswith(os.environ['FAIL_FETCH']):
        sys.exit(22)
    target = args[args.index('-o') + 1] if '-o' in args else next(x[3:] for x in args if x.startswith('-qO'))
    if target != '/dev/null':
        shutil.copyfile(os.environ['SOCKET_FIXTURE'], target)
if name == 'iptables' and args[0] == '-C':
    sys.exit(0 if os.environ.get('RULES_EXIST') else 1)
if name == os.environ.get('FAIL_COMMAND'):
    sys.exit(1)
''')
    recorder.chmod(0o755)
    for name in ("id", "curl", "wget", "systemctl", "mount", "iptables"):
        (fakebin / name).symlink_to(recorder)
    env = {**os.environ, "PATH": str(fakebin) + ":/usr/bin:/bin",
           "RECORD": str(tmp_path / "commands.jsonl"),
           "SOCKET_FIXTURE": str(SCRIPT.parent / "scripts" / "sshd.socket"),
           "RUNJAILED_SCRIPTS": str(tmp_path / "old-scripts"),
           "RUNJAILED_PROVISIONING": str(tmp_path / "provisioning"),
           "RUNJAILED_UNIT_DIR": str(tmp_path / "units"),
           "RUNJAILED_SYSTEMCTL": str(fakebin / "systemctl"),
           "RUNJAILED_MOUNT": str(fakebin / "mount"),
           "RUNJAILED_CURL": str(fakebin / "curl"),
           "RUNJAILED_IPTABLES": str(fakebin / "iptables"),
           "TMPDIR": str(tmp_path)}
    provisioning = tmp_path / "provisioning"
    provisioning.write_text('SYSTEM_ACCESS="locked"\n')

    def run(host="192.0.2.2", port="8883", **values):
        result = subprocess.run(["/bin/sh", str(ROBOT_SCRIPT), host, port],
                                env={**env, **values}, text=True, capture_output=True, timeout=5)
        log = Path(env["RECORD"])
        records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, records

    return SimpleNamespace(run=run, tmp=tmp_path, provisioning=provisioning, env=env)


def test_robot_script_aborts_without_root_before_download_or_service_changes(robot_shell):
    result, records = robot_shell.run(FAKE_UID="1000")
    assert result.returncode != 0
    assert "root" in result.stderr.lower()
    assert all(record[0] == "id" for record in records)


def test_robot_uses_curl_runtime_unit_and_only_callback_scoped_ssh_rules(robot_shell):
    result, records = robot_shell.run()
    assert result.returncode == 0, result.stderr
    assert any(record[0] == "curl" for record in records)
    assert not any(record[0] in ("wget", "mount") for record in records)
    assert robot_shell.provisioning.read_text() == 'SYSTEM_ACCESS="locked"\n'
    assert (robot_shell.tmp / "units" / "sshd.socket").read_bytes() == (
        SCRIPT.parent / "scripts" / "sshd.socket").read_bytes()
    assert [r[1:] for r in records if r[0] == "systemctl"] == [
        ["daemon-reload"], ["start", "sshd.socket"]]
    rules = [r[1:] for r in records if r[0] == "iptables"]
    assert rules == [
        ["-C", "INPUT", "-p", "tcp", "-s", "192.0.2.2", "--dport", "22", "-j", "ACCEPT"],
        ["-I", "INPUT", "1", "-p", "tcp", "-s", "192.0.2.2", "--dport", "22", "-j", "ACCEPT"],
        ["-C", "OUTPUT", "-p", "tcp", "-d", "192.0.2.2", "--sport", "22", "-j", "ACCEPT"],
        ["-I", "OUTPUT", "1", "-p", "tcp", "-d", "192.0.2.2", "--sport", "22", "-j", "ACCEPT"]]
    curls = [r for r in records if r[0] == "curl"]
    assert [r[-1] for r in curls] == [
        "http://192.0.2.2:8883/whoami/root",
        "http://192.0.2.2:8883/sshd.socket",
        "http://192.0.2.2:8883/done/ssh"]
    assert all("-fSs" in r and "--max-time" in r for r in curls)


@pytest.mark.parametrize("endpoint", ["whoami/root", "sshd.socket"])
def test_robot_download_failure_stops_before_service_or_firewall(robot_shell, endpoint):
    result, records = robot_shell.run(FAIL_FETCH=endpoint)
    assert result.returncode != 0
    assert not any(r[0] in ("systemctl", "iptables", "mount") for r in records)
    assert not any(r[-1].endswith("/done/ssh") for r in records)
    assert robot_shell.provisioning.read_text() == 'SYSTEM_ACCESS="locked"\n'


@pytest.mark.parametrize("host", ["bad;id", "1.2.3.999", "0.0.0.0", "1.2.3.4/24", "host.example"])
def test_robot_rejects_nonliteral_or_unsafe_callback_before_side_effects(robot_shell, host):
    result, records = robot_shell.run(host=host)
    assert result.returncode == 2
    assert not any(r[0] != "id" for r in records)


@pytest.mark.parametrize("port", ["0", "65536", "x;id"])
def test_robot_rejects_invalid_port_before_side_effects(robot_shell, port):
    result, records = robot_shell.run(port=port)
    assert result.returncode == 2
    assert not any(r[0] != "id" for r in records)
