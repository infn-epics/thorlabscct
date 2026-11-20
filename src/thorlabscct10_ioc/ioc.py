"""caproto-based Channel Access IOC for the Thorlabs CCT series."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

import numpy as np
from caproto import ChannelType
from caproto.server import PVGroup, pvproperty, run

from .backend import CCTBackend, SimulatedCCT, ThorlabsCCT
from .config import DEFAULT_SDK_PATH, ConfigError, DeviceConfig, IOCConfig, load_config

MAX_PIXELS = 4096
MAX_STRING = 39  # EPICS DBR_STRING holds 40 bytes including the terminator
MAX_ERROR = 240
log = logging.getLogger(__name__)


def padded(values: np.ndarray, length: int = MAX_PIXELS) -> np.ndarray:
    """Pad a waveform to ``length`` elements with NaN."""
    out = np.full(length, np.nan)
    out[:min(values.size, length)] = values[:length]
    return out


def padded_text(text: str, length: int = MAX_ERROR) -> str:
    """NUL-pad a char-waveform string to ``length`` characters."""
    return text[:length].ljust(length, "\0")


class CCTIOC(PVGroup):
    connected = pvproperty(value=0, dtype=ChannelType.ENUM, enum_strings=("Disconnected", "Connected"),
                           read_only=True, name="Connected")
    # DBR_STRING (max 39 characters): readable with a plain `caget`
    device_id = pvproperty(value="", dtype=ChannelType.STRING, read_only=True, name="DeviceId")
    last_error = pvproperty(value="", dtype=ChannelType.STRING, read_only=True, name="LastError")
    # full error text as a char waveform (`caget -S ...:LastErrorLong`), always
    # MAX_ERROR long (NUL padded): some CA clients (Phoebus/CAJ) cannot decode
    # arrays shorter than their declared length
    last_error_long = pvproperty(value="\0" * MAX_ERROR, max_length=MAX_ERROR, read_only=True,
                                 name="LastErrorLong")

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
        self._last_logged_error: str | None = None
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
            await self.device_id.write(value=str(self.backend.device_id)[:MAX_STRING])
            log.info("connected to %s", self.backend.device_id)
            await self._clear_error()
        except Exception as exc:
            await self._record_error(exc, "CCT connection failed")
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
            # no point queueing acquisitions (and errors) while disconnected
            if self.continuous.value and self.connected.value and self._acquire_event is not None:
                self._acquire_event.set()
            await asyncio.sleep(max(float(self.period.value), 0.03))

    async def _do_dark(self):
        assert self._lock is not None
        async with self._lock:
            await self.busy.write(value=1)
            try:
                await asyncio.to_thread(self.backend.acquire_dark)
                await self._clear_error()
            except Exception as exc:
                await self._record_error(exc, "dark acquisition failed")
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
                # publish full-length arrays (NaN after NumPixels): Phoebus/CAJ
                # fails to decode arrays shorter than the declared length
                await self.wavelength.write(value=padded(wavelength))
                await self.spectrum.write(value=padded(intensity))
                await self.num_pixels.write(value=count)
                await self.sequence.write(value=int(self.sequence.value) + 1)
                await self.timestamp.write(value=time.time())
                if count:
                    await self.peak_wavelength.write(value=float(wavelength[peak]))
                    await self.peak_intensity.write(value=float(intensity[peak]))
                await self._clear_error()
            except Exception as exc:
                await self._record_error(exc, "spectrum acquisition failed")
            finally:
                await self.busy.write(value=0)

    async def _record_error(self, exc: Exception, context: str | None = None):
        """Publish an error; log it (with traceback) only when it differs from the last one."""
        message = str(exc) or type(exc).__name__
        await self.last_error.write(value=message[:MAX_STRING])
        await self.last_error_long.write(value=padded_text(message))
        if context is None:
            return
        key = f"{context}: {message}"
        if key != self._last_logged_error:
            log.error("%s", context, exc_info=exc)
            self._last_logged_error = key
        else:
            log.debug("%s (repeated): %s", context, message)

    async def _clear_error(self):
        if self.last_error.value:
            await self.last_error.write(value="")
            await self.last_error_long.write(value=padded_text(""))
        if self._last_logged_error is not None:
            log.info("recovered after: %s", self._last_logged_error)
            self._last_logged_error = None

    async def _call_backend(self, function, *args):
        if not self.connected.value:
            raise RuntimeError("spectrometer is not connected")
        assert self._lock is not None
        async with self._lock:
            try:
                result = await asyncio.to_thread(function, *args)
                await self._clear_error()
                return result
            except Exception as exc:
                await self._record_error(exc)
                raise


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", metavar="YAML",
                        help="epik8s-style YAML instance file (iocprefix, server, devices, ...); "
                             "command-line options below override it")
    parser.add_argument("--prefix", help="EPICS PV prefix (default: CCT10:)")
    parser.add_argument("--simulate", action="store_true", default=None,
                        help="use the built-in CCT10 simulator")
    parser.add_argument("--sdk-path", help="directory containing the Thorlabs CCT .NET assemblies")
    parser.add_argument("--device-id", help="exact SDK connection key; defaults to first device found")
    parser.add_argument("--ip", help="register a CCT Ethernet IPv4 address before discovery")
    parser.add_argument("--virtual", action="store_true", default=None,
                        help="enable the Thorlabs SDK virtual device")
    parser.add_argument("--exposure", type=float, help="initial exposure in ms (default: 8.3)")
    parser.add_argument("--average", type=int, help="initial hardware average (default: 1)")
    parser.add_argument("--shutter", choices=("open", "closed"),
                        help="initial shutter state (default: open)")
    parser.add_argument("--continuous", action="store_true", default=None,
                        help="start continuous spectrum acquisition")
    parser.add_argument("--period", type=float,
                        help="initial continuous acquisition period in s (default: 1.0)")
    parser.add_argument("--interfaces", help="comma-separated IOC listen addresses (default: 0.0.0.0)")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="logging verbosity (default: INFO)")
    parser.add_argument("--log-pv-names", action="store_true", default=None,
                        help="log all PV names when the IOC starts")
    parser.add_argument("--list-pvs", action="store_true", help="print PV names and exit")
    return parser


def resolve_config(args: argparse.Namespace) -> IOCConfig:
    """Combine an optional YAML file with command-line overrides."""
    if args.config:
        config = load_config(args.config)
    else:
        config = IOCConfig(devices=[DeviceConfig(prefix="CCT10:",
                                                 sdk_path=os.environ.get("SDK_PATH", DEFAULT_SDK_PATH))])
    if args.interfaces is not None:
        config.interfaces = [item.strip() for item in args.interfaces.split(",") if item.strip()]
    if args.log_level is not None:
        config.log_level = args.log_level
    if args.log_pv_names:
        config.log_pv_names = True

    device_overrides = {
        "prefix": args.prefix, "ip": args.ip, "device_id": args.device_id,
        "sdk_path": args.sdk_path, "exposure": args.exposure, "average": args.average,
        "period": args.period, "simulate": args.simulate, "virtual": args.virtual,
        "continuous": args.continuous,
        "shutter_open": None if args.shutter is None else args.shutter == "open",
    }
    device_overrides = {key: value for key, value in device_overrides.items() if value is not None}
    if len(config.devices) > 1:
        for key in ("prefix", "ip", "device_id"):
            if key in device_overrides:
                raise ConfigError(f"--{key.replace('_', '-')} cannot be used with a multi-device configuration")
    for device in config.devices:
        for key, value in device_overrides.items():
            setattr(device, key, value)
        device.validate()
    if not config.interfaces:
        raise ConfigError("at least one listen interface is required")
    return config


def make_backend(device: DeviceConfig) -> CCTBackend:
    if device.simulate:
        return SimulatedCCT()
    if not device.sdk_path:
        raise ConfigError("an SDK path is required unless simulate is used")
    return ThorlabsCCT(device.sdk_path, device.device_id, device.ip, device.virtual)


def run_pyioc(instance) -> int:
    """Entrypoint for ``pyioc-run`` (epik8s-pyioc); ``instance`` is already validated."""
    devices = [
        DeviceConfig(
            prefix=device.prefix,
            name=device.name,
            ip=device.server,
            device_id=device.device_id,
            virtual=device.virtual,
            simulate=device.simulate,
            sdk_path=instance.sdk_path,
            exposure=device.exposure,
            average=device.average,
            shutter_open=device.shutter == "open",
            continuous=device.continuous,
            period=device.period,
        )
        for device in instance.devices
    ]
    config = IOCConfig(
        devices=devices,
        interfaces=[item.strip() for item in instance.interfaces.split(",") if item.strip()],
        log_level=instance.log_level,
        log_pv_names=instance.log_pv_names,
        name=instance.iocname,
    )
    serve(config)
    return 0


def serve(config: IOCConfig, list_pvs: bool = False) -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    backends: list[CCTBackend] = []
    pvdb: dict = {}
    for device in config.devices:
        backend = make_backend(device)
        backends.append(backend)
        ioc = CCTIOC(
            prefix=device.prefix,
            backend=backend,
            initial_exposure=device.exposure,
            initial_average=device.average,
            initial_shutter_open=device.shutter_open,
            initial_continuous=device.continuous,
            initial_period=device.period,
        )
        pvdb.update(ioc.pvdb)
        log.info("device %s: prefix=%s %s", device.name or "-", device.prefix,
                 "simulated" if device.simulate else f"ip={device.ip or 'discover'}")
    if list_pvs:
        print("\n".join(sorted(pvdb)))
        return
    try:
        run(pvdb, interfaces=config.interfaces, log_pv_names=config.log_pv_names)
    finally:
        for backend in backends:
            backend.close()


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    try:
        config = resolve_config(args)
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc
    serve(config, list_pvs=args.list_pvs)


if __name__ == "__main__":
    main()
