"""caproto-based Channel Access IOC for the Thorlabs CCT series."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

import numpy as np
from caproto import ChannelType
from caproto.server import PVGroup, pvproperty, run

from .backend import CCTBackend, SimulatedCCT, ThorlabsCCT

MAX_PIXELS = 4096
log = logging.getLogger(__name__)


class CCTIOC(PVGroup):
    connected = pvproperty(value=0, dtype=ChannelType.ENUM, enum_strings=("Disconnected", "Connected"),
                           read_only=True, name="Connected")
    device_id = pvproperty(value="", max_length=120, read_only=True, name="DeviceId")
    last_error = pvproperty(value="", max_length=240, read_only=True, name="LastError")

    exposure = pvproperty(value=8.3, dtype=float, name="Exposure", units="ms", precision=3,
                          lower_ctrl_limit=0.01, upper_ctrl_limit=30000.0)
    average = pvproperty(value=1, dtype=int, name="Average",
                         lower_ctrl_limit=1, upper_ctrl_limit=10000)
    shutter = pvproperty(value=1, dtype=ChannelType.ENUM, enum_strings=("Closed", "Open"),
                         name="Shutter")

    acquire = pvproperty(value=0, dtype=ChannelType.ENUM, enum_strings=("Idle", "Acquire"),
                         name="Acquire")
    acquire_dark = pvproperty(value=0, dtype=ChannelType.ENUM, enum_strings=("Idle", "Acquire"),
                              name="AcquireDark")
    continuous = pvproperty(value=0, dtype=ChannelType.ENUM, enum_strings=("Off", "On"),
                            name="Continuous")
    period = pvproperty(value=1.0, dtype=float, name="Period", units="s", precision=3,
                        lower_ctrl_limit=0.03, upper_ctrl_limit=3600.0)
    busy = pvproperty(value=0, dtype=ChannelType.ENUM, enum_strings=("Idle", "Acquiring"),
                      read_only=True, name="Busy")

    wavelength = pvproperty(value=np.zeros(MAX_PIXELS), dtype=float, max_length=MAX_PIXELS,
                            read_only=True, name="Wavelength", units="nm", precision=4)
    spectrum = pvproperty(value=np.zeros(MAX_PIXELS), dtype=float, max_length=MAX_PIXELS,
                          read_only=True, name="Spectrum", units="counts", precision=3)
    num_pixels = pvproperty(value=0, dtype=int, read_only=True, name="NumPixels")
    sequence = pvproperty(value=0, dtype=int, read_only=True, name="Sequence")
    timestamp = pvproperty(value=0.0, dtype=float, read_only=True, name="Timestamp", units="s")
    peak_wavelength = pvproperty(value=0.0, dtype=float, read_only=True, name="PeakWavelength",
                                 units="nm", precision=4)
    peak_intensity = pvproperty(value=0.0, dtype=float, read_only=True, name="PeakIntensity",
                                units="counts", precision=3)

    def __init__(
        self,
        *args,
        backend: CCTBackend,
        initial_exposure: float = 8.3,
        initial_average: int = 1,
        initial_shutter_open: bool = True,
        initial_continuous: bool = False,
        initial_period: float = 1.0,
        **kwargs,
    ):
        self.backend = backend
        self.initial_exposure = initial_exposure
        self.initial_average = initial_average
        self.initial_shutter_open = initial_shutter_open
        self.initial_continuous = initial_continuous
        self.initial_period = initial_period
        self._lock: asyncio.Lock | None = None
        self._acquire_event: asyncio.Event | None = None
        self._dark_event: asyncio.Event | None = None
        super().__init__(*args, **kwargs)

    @connected.startup
    async def connected(self, instance, async_lib):
        self._lock = asyncio.Lock()
        self._acquire_event = asyncio.Event()
        self._dark_event = asyncio.Event()
        # Seed runtime-configured values without invoking client put handlers.
        await self.exposure.write(value=self.initial_exposure, verify_value=False)
        await self.average.write(value=self.initial_average, verify_value=False)
        await self.shutter.write(value=int(self.initial_shutter_open), verify_value=False)
        await self.continuous.write(value=int(self.initial_continuous), verify_value=False)
        await self.period.write(value=self.initial_period, verify_value=False)
        try:
            await asyncio.to_thread(self.backend.connect)
            await asyncio.to_thread(self.backend.set_exposure, float(self.exposure.value))
            await asyncio.to_thread(self.backend.set_average, int(self.average.value))
            await asyncio.to_thread(self.backend.set_shutter, bool(self.shutter.value))
            await instance.write(value=1)
            await self.device_id.write(value=self.backend.device_id)
            await self.last_error.write(value="")
        except Exception as exc:
            await self._record_error(exc)
            log.exception("CCT connection failed")
            await asyncio.to_thread(self.backend.close)
        asyncio.create_task(self._worker())
        asyncio.create_task(self._continuous_worker())

    @exposure.putter
    async def exposure(self, instance, value):
        if not 0.01 <= value <= 30000.0:
            raise ValueError("Exposure must be 0.01 .. 30000 ms")
        await self._call_backend(self.backend.set_exposure, value)
        return value

    @average.putter
    async def average(self, instance, value):
        if not 1 <= value <= 10000:
            raise ValueError("Average must be 1 .. 10000 frames")
        await self._call_backend(self.backend.set_average, value)
        return value

    @shutter.putter
    async def shutter(self, instance, value):
        await self._call_backend(self.backend.set_shutter, bool(value))
        return value

    @acquire.putter
    async def acquire(self, instance, value):
        if value and self._acquire_event is not None:
            self._acquire_event.set()
        return 0

    @acquire_dark.putter
    async def acquire_dark(self, instance, value):
        if value and self._dark_event is not None:
            self._dark_event.set()
        return 0

    async def _worker(self):
        assert self._acquire_event is not None and self._dark_event is not None
        while True:
            acquire_wait = asyncio.create_task(self._acquire_event.wait())
            dark_wait = asyncio.create_task(self._dark_event.wait())
            done, pending = await asyncio.wait(
                (acquire_wait, dark_wait), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if dark_wait in done:
                self._dark_event.clear()
                await self._do_dark()
            if acquire_wait in done:
                self._acquire_event.clear()
                await self._do_acquire()

    async def _continuous_worker(self):
        while True:
            if self.continuous.value and self._acquire_event is not None:
                self._acquire_event.set()
            await asyncio.sleep(max(float(self.period.value), 0.03))

    async def _do_dark(self):
        assert self._lock is not None
        async with self._lock:
            await self.busy.write(value=1)
            try:
                await asyncio.to_thread(self.backend.acquire_dark)
                await self.last_error.write(value="")
            except Exception as exc:
                await self._record_error(exc)
                log.exception("dark acquisition failed")
            finally:
                await self.busy.write(value=0)

    async def _do_acquire(self):
        assert self._lock is not None
        async with self._lock:
            await self.busy.write(value=1)
            try:
                result = await asyncio.to_thread(self.backend.acquire)
                count = min(result.wavelength.size, result.intensity.size, MAX_PIXELS)
                wavelength = result.wavelength[:count]
                intensity = result.intensity[:count]
                peak = int(np.argmax(intensity)) if count else 0
                await self.wavelength.write(value=wavelength)
                await self.spectrum.write(value=intensity)
                await self.num_pixels.write(value=count)
                await self.sequence.write(value=int(self.sequence.value) + 1)
                await self.timestamp.write(value=time.time())
                if count:
                    await self.peak_wavelength.write(value=float(wavelength[peak]))
                    await self.peak_intensity.write(value=float(intensity[peak]))
                await self.last_error.write(value="")
            except Exception as exc:
                await self._record_error(exc)
                log.exception("spectrum acquisition failed")
            finally:
                await self.busy.write(value=0)

    async def _record_error(self, exc: Exception):
        await self.last_error.write(value=str(exc)[:240])

    async def _call_backend(self, function, *args):
        if not self.connected.value:
            raise RuntimeError("spectrometer is not connected")
        assert self._lock is not None
        async with self._lock:
            try:
                result = await asyncio.to_thread(function, *args)
                await self.last_error.write(value="")
                return result
            except Exception as exc:
                await self._record_error(exc)
                raise


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="CCT10:", help="EPICS PV prefix (default: %(default)s)")
    parser.add_argument("--simulate", action="store_true", help="use the built-in CCT10 simulator")
    parser.add_argument("--sdk-path", help="directory containing the Thorlabs CCT .NET assemblies")
    parser.add_argument("--device-id", help="exact SDK connection key; defaults to first device found")
    parser.add_argument("--ip", help="register a CCT Ethernet IPv4 address before discovery")
    parser.add_argument("--virtual", action="store_true", help="enable the Thorlabs SDK virtual device")
    parser.add_argument("--exposure", type=float, default=8.3,
                        help="initial exposure in ms (default: %(default)s)")
    parser.add_argument("--average", type=int, default=1,
                        help="initial hardware average (default: %(default)s)")
    parser.add_argument("--shutter", choices=("open", "closed"), default="open",
                        help="initial shutter state (default: %(default)s)")
    parser.add_argument("--continuous", action="store_true",
                        help="start continuous spectrum acquisition")
    parser.add_argument("--period", type=float, default=1.0,
                        help="initial continuous acquisition period in s (default: %(default)s)")
    parser.add_argument("--interfaces", default="0.0.0.0",
                        help="comma-separated IOC listen addresses (default: %(default)s)")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        default="INFO", help="logging verbosity (default: %(default)s)")
    parser.add_argument("--log-pv-names", action="store_true",
                        help="log all PV names when the IOC starts")
    parser.add_argument("--list-pvs", action="store_true", help="print PV names and exit")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    if not 0.01 <= args.exposure <= 30000.0:
        raise SystemExit("--exposure must be in the range 0.01 .. 30000 ms")
    if not 1 <= args.average <= 10000:
        raise SystemExit("--average must be in the range 1 .. 10000")
    if not 0.03 <= args.period <= 3600.0:
        raise SystemExit("--period must be in the range 0.03 .. 3600 s")
    interfaces = [item.strip() for item in args.interfaces.split(",") if item.strip()]
    if not interfaces:
        raise SystemExit("--interfaces must contain at least one address")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.simulate:
        backend: CCTBackend = SimulatedCCT()
    else:
        if not args.sdk_path:
            raise SystemExit("--sdk-path is required unless --simulate is used")
        backend = ThorlabsCCT(args.sdk_path, args.device_id, args.ip, args.virtual)
    ioc = CCTIOC(
        prefix=args.prefix,
        backend=backend,
        initial_exposure=args.exposure,
        initial_average=args.average,
        initial_shutter_open=args.shutter == "open",
        initial_continuous=args.continuous,
        initial_period=args.period,
    )
    if args.list_pvs:
        print("\n".join(sorted(ioc.pvdb)))
        return
    try:
        run(ioc.pvdb, interfaces=interfaces, log_pv_names=args.log_pv_names)
    finally:
        backend.close()


if __name__ == "__main__":
    main()
