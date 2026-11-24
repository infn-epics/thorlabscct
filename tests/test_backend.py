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

    from thorlabscct10_ioc.ioc import CCTIOC, MAX_ERROR, MAX_STRING

    async def scenario():
        ioc = CCTIOC(prefix="T:", backend=SimulatedCCT())
        assert ioc.device_id.data_type == ChannelType.STRING
        assert ioc.last_error.data_type == ChannelType.STRING
        long = RuntimeError("x" * 100)
        for _ in range(5):
            await ioc._record_error(long, "spectrum acquisition failed")
        assert ioc.last_error.value == "x" * MAX_STRING
        assert ioc.last_error_long.value.rstrip("\0") == "x" * 100
        assert len(ioc.last_error_long.value) == MAX_ERROR
        await ioc._clear_error()
        assert ioc.last_error.value == "" and ioc.last_error_long.value.rstrip("\0") == ""
        assert len(ioc.last_error_long.value) == MAX_ERROR
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


def test_waveforms_are_always_full_length():
    import asyncio

    from thorlabscct10_ioc.ioc import CCTIOC, MAX_PIXELS

    async def scenario():
        ioc = CCTIOC(prefix="T:", backend=SimulatedCCT(pixels=2048))
        ioc._lock = asyncio.Lock()
        ioc.backend.connect()
        await ioc._do_acquire()
        return ioc

    ioc = asyncio.run(scenario())
    assert len(ioc.wavelength.value) == MAX_PIXELS and len(ioc.spectrum.value) == MAX_PIXELS
    assert ioc.num_pixels.value == 2048
    assert np.isnan(ioc.spectrum.value[2048:]).all() and not np.isnan(ioc.spectrum.value[:2048]).any()


def test_simulated_hardware_trigger_waits_times_out_and_aborts():
    import threading
    import time

    from thorlabscct10_ioc.backend import AcquisitionAborted, TriggerTimeout

    cct = SimulatedCCT()
    cct.connect()
    cct.set_input_trigger(True, False, False)
    started = time.monotonic()
    with pytest.raises(TriggerTimeout):
        cct.acquire(timeout=0.2)
    assert 0.15 < time.monotonic() - started < 1.0

    threading.Timer(0.1, cct.fire_trigger).start()
    assert cct.acquire(timeout=2).triggered is True

    threading.Timer(0.1, cct.abort).start()
    with pytest.raises(AcquisitionAborted):
        cct.acquire(timeout=0)          # 0 = wait forever, ended only by abort


def test_simulated_per_frame_averaging_needs_one_edge_per_frame():
    import threading

    from thorlabscct10_ioc.backend import TriggerTimeout

    cct = SimulatedCCT()
    cct.connect()
    cct.set_average(3)
    cct.set_input_trigger(True, False, False)          # per frame
    for delay in (0.05, 0.1):
        threading.Timer(delay, cct.fire_trigger).start()
    with pytest.raises(TriggerTimeout):                 # only 2 of 3 edges
        cct.acquire(timeout=0.4)
    cct.set_input_trigger(True, True, False)            # single trigger
    threading.Timer(0.05, cct.fire_trigger).start()
    assert cct.acquire(timeout=2).triggered


def test_free_running_spectrum_is_not_triggered():
    cct = SimulatedCCT()
    cct.connect()
    assert cct.acquire().triggered is False


def test_devices_without_trigger_support_stay_connected(caplog):
    import asyncio
    import logging

    from thorlabscct10_ioc.ioc import CCTIOC

    class NoTrigger(SimulatedCCT):
        def set_input_trigger(self, *args):
            raise NotImplementedError("The method or operation is not implemented.")

        def set_output_trigger_delay(self, milliseconds):
            raise NotImplementedError("The method or operation is not implemented.")

    async def scenario(**initial):
        ioc = CCTIOC(prefix="T:", backend=NoTrigger(), **initial)
        await CCTIOC.connected.pvspec.startup(ioc, ioc.connected, None)
        return ioc

    with caplog.at_level(logging.WARNING, logger="thorlabscct10_ioc.ioc"):
        ioc = asyncio.run(scenario())                    # free running: only a warning
    assert ioc.connected.value in (1, "Connected") and ioc.last_error.value == ""
    assert any("free running only" in r.getMessage() for r in caplog.records)

    ioc = asyncio.run(scenario(initial_trigger_hardware=True))   # requested: reported
    assert ioc.connected.value in (1, "Connected")
    assert "trigger not available" in ioc.last_error.value
    assert ioc.trigger_mode.value in (0, "Free running")


def test_supervisor_reconnects_and_restores_settings(monkeypatch):
    import asyncio

    from thorlabscct10_ioc.ioc import CCTIOC

    class Flaky(SimulatedCCT):
        """Refuses the first connection (a session the killed process still holds), then works."""

        def __init__(self):
            super().__init__()
            self.attempts = 0
            self.trigger_calls = []

        def connect(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("no CCT spectrometer was discovered")
            super().connect()

        def set_input_trigger(self, *args):
            self.trigger_calls.append(args)
            super().set_input_trigger(*args)

    monkeypatch.setattr(CCTIOC, "RECONNECT_PERIOD", 0.05)

    async def scenario():
        backend = Flaky()
        ioc = CCTIOC(prefix="T:", backend=backend)
        await CCTIOC.connected.pvspec.startup(ioc, ioc.connected, None)
        assert ioc.connected.value in (0, "Disconnected") and "discovered" in ioc.last_error.value
        await ioc.trigger_mode.write(value=1, verify_value=False)      # an operator chose Hardware meanwhile
        await asyncio.sleep(0.3)
        assert ioc.connected.value in (1, "Connected") and ioc.last_error.value == ""
        assert backend.trigger_calls[-1][0] is True                     # restored on reconnect
        assert ioc.memory_used.value > 0
        # the device stops answering: after MAX_FAILURES acquisitions, reconnect
        ioc._failures = ioc.MAX_FAILURES
        before = backend.attempts
        await asyncio.sleep(0.3)
        assert backend.attempts == before + 1 and ioc._failures == 0
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()

    asyncio.run(scenario())


def test_frame_timing_from_the_device_clock():
    import asyncio

    from thorlabscct10_ioc.ioc import CCTIOC

    async def scenario():
        ioc = CCTIOC(prefix="T:", backend=SimulatedCCT())
        for tick in (1_000_000, 1_100_000, 1_150_000, 2_150_000):   # 100, 50 and 1000 ms apart
            await ioc._publish_frame_timing(tick)
        return ioc

    ioc = asyncio.run(scenario())
    assert ioc.device_time.value == 2.15
    assert ioc.frame_interval.value == 1000.0
    assert (ioc.frame_interval_min.value, ioc.frame_interval_max.value) == (50.0, 1000.0)
    ioc._reset_frame_timing()
    assert ioc._last_tick is None and ioc._intervals == []
