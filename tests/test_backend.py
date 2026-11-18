import numpy as np
import pytest

from thorlabscct10_ioc.backend import SimulatedCCT, resolve_ipv4
from thorlabscct10_ioc.ioc import build_arg_parser


def test_simulated_spectrum_shape_and_peak():
    cct = SimulatedCCT(seed=1)
    cct.connect()
    spectrum = cct.acquire()
    assert spectrum.wavelength.shape == (2048,)
    assert spectrum.intensity.shape == (2048,)
    assert np.all(np.diff(spectrum.wavelength) > 0)
    assert 525 < spectrum.wavelength[np.argmax(spectrum.intensity)] < 540


def test_dark_and_shutter_reduce_signal():
    cct = SimulatedCCT(seed=2)
    cct.connect()
    bright = cct.acquire().intensity.mean()
    cct.acquire_dark()
    cct.set_shutter(False)
    dark = cct.acquire().intensity.mean()
    assert dark < bright


def test_dark_capture_does_not_change_simulated_shutter():
    cct = SimulatedCCT()
    cct.connect()
    cct.set_shutter(False)
    cct.acquire_dark()
    assert cct.shutter_open is False


@pytest.mark.parametrize("value", [0, 10001])
def test_average_limits(value):
    with pytest.raises(ValueError):
        SimulatedCCT().set_average(value)


@pytest.mark.parametrize("value", [0.0, 30000.1])
def test_exposure_limits(value):
    with pytest.raises(ValueError):
        SimulatedCCT().set_exposure(value)


def test_container_facing_startup_options_parse():
    args = build_arg_parser().parse_args([
        "--prefix", "LAB:CCT10:",
        "--ip", "192.168.0.160",
        "--exposure", "12.5",
        "--average", "4",
        "--shutter", "closed",
        "--continuous",
        "--period", "0.5",
        "--interfaces", "0.0.0.0,127.0.0.1",
    ])
    assert args.prefix == "LAB:CCT10:"
    assert args.ip == "192.168.0.160"
    assert args.exposure == 12.5
    assert args.average == 4
    assert args.shutter == "closed"
    assert args.continuous is True


def test_resolve_ipv4_accepts_literals_and_hostnames():
    assert resolve_ipv4(" 192.168.104.109 ") == "192.168.104.109"
    assert resolve_ipv4("localhost") == "127.0.0.1"
    with pytest.raises(RuntimeError):
        resolve_ipv4("no-such-host.invalid")


def test_error_pvs_are_strings_and_repeated_errors_log_once(caplog):
    import asyncio
    import logging

    from caproto import ChannelType

    from thorlabscct10_ioc.ioc import CCTIOC, MAX_STRING

    async def scenario():
        ioc = CCTIOC(prefix="T:", backend=SimulatedCCT())
        assert ioc.device_id.data_type == ChannelType.STRING
        assert ioc.last_error.data_type == ChannelType.STRING
        long = RuntimeError("x" * 100)
        for _ in range(5):
            await ioc._record_error(long, "spectrum acquisition failed")
        assert ioc.last_error.value == "x" * MAX_STRING
        assert ioc.last_error_long.value == "x" * 100
        await ioc._clear_error()
        assert ioc.last_error.value == "" and ioc.last_error_long.value == ""
        await ioc._record_error(long, "spectrum acquisition failed")   # logged again after recovery

    with caplog.at_level(logging.DEBUG, logger="thorlabscct10_ioc.ioc"):
        asyncio.run(scenario())
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert any("recovered after" in r.getMessage() for r in caplog.records)


def test_continuous_does_not_queue_acquisitions_while_disconnected():
    import asyncio

    from thorlabscct10_ioc.ioc import CCTIOC

    async def scenario():
        ioc = CCTIOC(prefix="T:", backend=SimulatedCCT())
        ioc._acquire_event = asyncio.Event()
        await ioc.continuous.write(value=1, verify_value=False)
        await ioc.period.write(value=0.03, verify_value=False)
        task = asyncio.create_task(ioc._continuous_worker())
        await asyncio.sleep(0.1)
        queued_disconnected = ioc._acquire_event.is_set()
        await ioc.connected.write(value=1)
        await asyncio.sleep(0.1)
        task.cancel()
        return queued_disconnected, ioc._acquire_event.is_set()

    disconnected, connected = asyncio.run(scenario())
    assert disconnected is False and connected is True
