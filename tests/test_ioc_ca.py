"""Channel Access integration tests: a simulated IOC in a subprocess and a CA client."""

import os
import socket
import subprocess
import sys
import time

import pytest

caproto_client = pytest.importorskip("caproto.sync.client")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def ioc(monkeypatch):
    """Yield (get, put); enum PVs read back by name, e.g. b"Hardware"."""
    port = _free_port()
    env = dict(os.environ, EPICS_CA_SERVER_PORT=str(port), EPICS_CA_ADDR_LIST="127.0.0.1",
               EPICS_CA_AUTO_ADDR_LIST="NO", EPICS_CAS_INTF_ADDR_LIST="127.0.0.1")
    for key in ("EPICS_CA_SERVER_PORT", "EPICS_CA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST"):
        monkeypatch.setenv(key, env[key])
    proc = subprocess.Popen(
        [sys.executable, "-m", "thorlabscct10_ioc.ioc", "--simulate", "--prefix", "T:",
         "--interfaces", "127.0.0.1", "--period", "0.05", "--trigger-timeout", "0.3"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def get(pv, **kw):
        return caproto_client.read(f"T:{pv}", timeout=2, **kw).data[0]

    def put(pv, value):
        caproto_client.write(f"T:{pv}", value, timeout=2, notify=True)

    try:
        for _ in range(100):
            try:
                if get("Connected") == b"Connected":
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("IOC did not start")
        yield get, put
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_enum_writes_by_name_are_honoured(ioc):
    """Regression: enum puts arrive as strings ('Off', 'Idle', 'Closed')."""
    get, put = ioc
    put("Continuous", "On")
    first = get("Sequence")
    assert _wait(lambda: get("Sequence") > first + 2)
    put("Continuous", "Off")                           # used to keep acquiring ("Off" is truthy)
    time.sleep(0.3)
    stopped = get("Sequence")
    time.sleep(0.4)
    assert get("Sequence") == stopped
    put("Acquire", "Idle")                             # used to start an acquisition
    time.sleep(0.3)
    assert get("Sequence") == stopped
    put("Acquire", "Acquire")
    assert _wait(lambda: get("Sequence") == stopped + 1)


def test_hardware_trigger_timeout_abort_and_settings_while_armed(ioc):
    get, put = ioc
    put("TriggerMode", "Hardware")
    assert get("TriggerMode") == b"Hardware"
    seq = get("Sequence")
    put("Acquire", "Acquire")                          # no trigger arrives -> timeout
    assert _wait(lambda: get("TriggerArmed") == b"Waiting for trigger", 1.0)
    assert _wait(lambda: b"no input trigger" in get("LastError"), 2.0)
    assert get("TriggerArmed") == b"Idle" and get("Sequence") == seq

    put("TriggerTimeout", 0)                           # wait forever ...
    put("Acquire", "Acquire")
    assert _wait(lambda: get("TriggerArmed") == b"Waiting for trigger", 1.0)
    started = time.monotonic()
    put("Exposure", 20.0)                              # ... a setting change must not block
    assert time.monotonic() - started < 1.5
    assert get("Exposure") == 20.0
    assert _wait(lambda: get("TriggerArmed") == b"Idle", 1.0)

    put("Acquire", "Acquire")
    assert _wait(lambda: get("TriggerArmed") == b"Waiting for trigger", 1.0)
    put("Abort", "Abort")
    assert _wait(lambda: get("TriggerArmed") == b"Idle", 1.0)
    assert get("Sequence") == seq

    put("TriggerMode", "Free running")
    put("Acquire", "Acquire")
    assert _wait(lambda: get("Sequence") == seq + 1)
    assert get("Triggered") == b"No"


def test_sigterm_releases_the_device_and_exits(tmp_path):
    import signal

    proc = subprocess.Popen([sys.executable, "-m", "thorlabscct10_ioc.ioc", "--simulate", "--prefix", "TERM:",
                             "--interfaces", "127.0.0.1"],
                            env=dict(os.environ, EPICS_CA_SERVER_PORT=str(_free_port())),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)
    assert "device sessions released" in out
