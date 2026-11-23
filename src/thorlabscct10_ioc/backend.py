"""Hardware and simulation backends for Thorlabs CCT spectrometers."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
from pathlib import Path
import math
import os
import socket
import sys
import threading
import time
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Spectrum:
    wavelength: np.ndarray
    intensity: np.ndarray
    triggered: bool = False   # acquisition started by the input hardware trigger


class TriggerTimeout(TimeoutError):
    """No input hardware trigger arrived within the acquisition timeout."""


class AcquisitionAborted(RuntimeError):
    """The pending acquisition was cancelled by abort()."""


def resolve_ipv4(address: str) -> str:
    """Return an IPv4 literal for ``address`` (an IPv4 address or a hostname).

    The Thorlabs SDK registers Ethernet devices by IP address only.
    """
    address = address.strip()
    try:
        return str(ipaddress.IPv4Address(address))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(address, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RuntimeError(f"cannot resolve CCT address {address!r}: {exc}") from exc
    resolved = infos[0][4][0]
    log.info("resolved CCT address %s -> %s", address, resolved)
    return resolved


class CCTBackend(Protocol):
    device_id: str

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def set_exposure(self, milliseconds: float) -> None: ...
    def set_average(self, frames: int) -> None: ...
    def set_shutter(self, opened: bool) -> None: ...
    def acquire_dark(self) -> None: ...
    def set_input_trigger(self, enabled: bool, single_trigger_average: bool,
                          falling_edge: bool) -> None: ...
    def set_output_trigger_delay(self, milliseconds: float) -> None: ...
    def acquire(self, timeout: float | None = None) -> Spectrum: ...
    def abort(self) -> None: ...


class SimulatedCCT:
    """Deterministic CCT10-like simulator, useful without EPICS or vendor software."""

    device_id = "SIM:CCT10"

    def __init__(self, pixels: int = 2048, seed: int = 10, trigger_rate: float = 0.0):
        self.pixels = pixels
        self.exposure_ms = 8.3
        self.average = 1
        self.shutter_open = True
        self.connected = False
        # input hardware trigger emulation: edges come from fire_trigger() or,
        # with trigger_rate > 0 Hz, from an internal pulse generator
        self.trigger_enabled = False
        self.single_trigger_average = False
        self.falling_edge = False
        self.output_delay_ms = 0.0
        self.trigger_rate = float(trigger_rate)
        self._edges = threading.Semaphore(0)
        self._armed = threading.Event()
        self._abort = threading.Event()
        self._stop = threading.Event()
        self._generator: threading.Thread | None = None
        self._rng = np.random.default_rng(seed)
        self._dark = np.zeros(pixels)

    def connect(self) -> None:
        self.connected = True
        if self.trigger_rate > 0 and self._generator is None:
            self._stop.clear()
            self._generator = threading.Thread(target=self._pulse_generator, daemon=True)
            self._generator.start()

    def close(self) -> None:
        self.connected = False
        self._stop.set()
        self.abort()
        self._generator = None

    def _pulse_generator(self) -> None:
        while not self._stop.wait(1.0 / self.trigger_rate):
            self.fire_trigger()

    def fire_trigger(self) -> None:
        """Emulate one TTL edge on the trigger input (lost if nothing is armed)."""
        if self._armed.is_set():
            self._edges.release()

    def set_input_trigger(self, enabled: bool, single_trigger_average: bool,
                          falling_edge: bool) -> None:
        self.trigger_enabled = bool(enabled)
        self.single_trigger_average = bool(single_trigger_average)
        self.falling_edge = bool(falling_edge)

    def set_output_trigger_delay(self, milliseconds: float) -> None:
        self.output_delay_ms = float(milliseconds)

    def abort(self) -> None:
        self._abort.set()

    def _wait_for_edges(self, count: int, timeout: float | None) -> None:
        deadline = None if not timeout else time.monotonic() + timeout
        while self._edges.acquire(blocking=False):   # drop edges from before arming
            pass
        self._armed.set()
        try:
            received = 0
            while received < count:
                if self._abort.is_set():
                    raise AcquisitionAborted("acquisition aborted")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TriggerTimeout(f"no input trigger within {timeout:g} s")
                if self._edges.acquire(timeout=0.01):
                    received += 1
        finally:
            self._armed.clear()

    def set_exposure(self, milliseconds: float) -> None:
        if not 0.01 <= milliseconds <= 30_000:
            raise ValueError("exposure must be in the range 0.01 to 30000 ms")
        self.exposure_ms = float(milliseconds)

    def set_average(self, frames: int) -> None:
        if not 1 <= frames <= 10_000:
            raise ValueError("average must be in the range 1 to 10000 frames")
        self.average = int(frames)

    def set_shutter(self, opened: bool) -> None:
        self.shutter_open = bool(opened)

    def acquire_dark(self) -> None:
        self._require_connected()
        self._dark = self._rng.normal(120.0, 4.0, self.pixels)

    def acquire(self, timeout: float | None = None) -> Spectrum:
        self._require_connected()
        self._abort.clear()
        if self.trigger_enabled:
            # per-frame averaging needs one edge per averaged frame
            self._wait_for_edges(1 if self.single_trigger_average else self.average, timeout)
        wavelength = np.linspace(200.0, 1000.0, self.pixels)
        noise = self._rng.normal(0.0, 12.0 / math.sqrt(self.average), self.pixels)
        signal = np.zeros(self.pixels)
        if self.shutter_open:
            scale = min(self.exposure_ms / 8.3, 20.0)
            signal = scale * (
                2500.0 * np.exp(-0.5 * ((wavelength - 532.0) / 3.0) ** 2)
                + 1500.0 * np.exp(-0.5 * ((wavelength - 632.8) / 5.0) ** 2)
                + 300.0
            )
        intensity = np.clip(120.0 + signal + noise - self._dark, 0.0, 65535.0)
        time.sleep(min(self.exposure_ms * self.average / 1000.0, 0.05))
        return Spectrum(wavelength, intensity, triggered=self.trigger_enabled)

    def _require_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("spectrometer is not connected")


class ThorlabsCCT:
    """Adapter for the Thorlabs CCT .NET instrument driver.

    The SDK is proprietary and must be installed separately.  The calls here follow
    Thorlabs' official ``CCT_example_pythonnet.py`` example.
    """

    def __init__(
        self,
        sdk_path: str | os.PathLike[str],
        device_id: str | None = None,
        ip_address: str | None = None,
        virtual: bool = False,
    ):
        self.sdk_path = Path(sdk_path).expanduser().resolve()
        self.requested_device_id = device_id
        self.ip_address = ip_address
        self.virtual = virtual
        self.device_id = ""
        self._helper = None
        self._device = None
        self._token = None
        self._shutter_open = True
        self._cts_type = None
        self._acquire_cts = None
        self._aborted = False
        self._cts_lock = threading.Lock()
        self._trigger = (False, False, False)   # enabled, single-trigger averaging, falling edge

    def connect(self) -> None:
        if not self.sdk_path.is_dir():
            raise FileNotFoundError(f"CCT SDK directory does not exist: {self.sdk_path}")
        path = str(self.sdk_path)
        if path not in sys.path:
            sys.path.append(path)
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")

        try:
            import clr  # type: ignore

            clr.AddReference(str(self.sdk_path / "Thorlabs.ManagedDevice.CompactSpectrographDriver"))
            clr.AddReference(str(self.sdk_path / "Microsoft.Extensions.Logging.Abstractions"))
            from Microsoft.Extensions.Logging.Abstractions import NullLoggerFactory  # type: ignore
            from System.Threading import CancellationTokenSource  # type: ignore
            from Thorlabs.ManagedDevice.CompactSpectrographDriver.Workflow import (  # type: ignore
                StartupHelperCompactSpectrometer,
            )
        except Exception as exc:
            raise RuntimeError(
                "unable to load the Thorlabs CCT .NET driver; check --sdk-path and pythonnet"
            ) from exc

        self._helper = StartupHelperCompactSpectrometer(NullLoggerFactory.Instance.CreateLogger("CCTIOC"))
        if self.ip_address:
            self._helper.RegisterEthernetIpAddress(resolve_ipv4(self.ip_address))
        self._helper.WithVirtual = self.virtual
        self._cts_type = CancellationTokenSource
        self._token = CancellationTokenSource().Token
        known = [str(key) for key in self._helper.GetKnownDevicesAsync(self._token).Result]
        if not known:
            self.close()
            raise RuntimeError("no CCT spectrometer was discovered")
        if self.requested_device_id:
            match = next((key for key in known if key == self.requested_device_id), None)
            if match is None:
                self.close()
                raise RuntimeError(
                    f"device {self.requested_device_id!r} not found; discovered: {', '.join(known)}"
                )
            self.device_id = match
        else:
            self.device_id = known[0]
        self._device = self._helper.GetCompactSpectrographById(self.device_id)

    def close(self) -> None:
        if self._helper is not None:
            self._helper.Dispose()
        self._helper = self._device = self._token = None

    def set_exposure(self, milliseconds: float) -> None:
        self._check(self._spectrometer.SetManualExposureAsync(float(milliseconds), self._token).Result,
                    "set exposure")

    def set_average(self, frames: int) -> None:
        self._check(self._spectrometer.SetHwAverageAsync(int(frames), self._token).Result,
                    "set hardware average")

    def set_shutter(self, opened: bool) -> None:
        self._check(self._spectrometer.SetShutterAsync(bool(opened), self._token).Result,
                    "set shutter")
        self._shutter_open = bool(opened)
        time.sleep(0.04)

    def set_input_trigger(self, enabled: bool, single_trigger_average: bool,
                          falling_edge: bool) -> None:
        settings = (bool(enabled), bool(single_trigger_average), bool(falling_edge))
        self._check(self._spectrometer.SetInputHwTriggerAsync(*settings, self._token).Result,
                    "set input hardware trigger")
        self._trigger = settings

    def set_output_trigger_delay(self, milliseconds: float) -> None:
        self._check(self._spectrometer.SetOutputHwTriggerDelayAsync(float(milliseconds),
                                                                    self._token).Result,
                    "set output hardware trigger delay")

    def acquire_dark(self) -> None:
        # the dark reference does not depend on beam timing: take it free running
        triggered = self._trigger[0]
        if triggered:
            self.set_input_trigger(False, self._trigger[1], self._trigger[2])
        restore_open = self._shutter_open
        self.set_shutter(False)
        try:
            self._check(self._spectrometer.UpdateDarkSpectrumAsync(False, self._token).Result,
                        "acquire dark spectrum")
        finally:
            self.set_shutter(restore_open)
            if triggered:
                self.set_input_trigger(True, self._trigger[1], self._trigger[2])

    def acquire(self, timeout: float | None = None) -> Spectrum:
        """Acquire one spectrum; in trigger mode wait at most ``timeout`` s (None/0: forever)."""
        cts = self._cts_type()
        if timeout and self._trigger[0]:
            cts.CancelAfter(max(1, int(timeout * 1000)))
        with self._cts_lock:
            self._acquire_cts, self._aborted = cts, False
        try:
            try:
                result = self._spectrometer.AcquireSingleSpectrumAsync(cts.Token).Result
            except Exception:
                if cts.IsCancellationRequested:
                    result = None
                else:
                    raise
            if result is None:
                if self._aborted:
                    raise AcquisitionAborted("acquisition aborted")
                if cts.IsCancellationRequested:
                    raise TriggerTimeout(f"no input trigger within {timeout:g} s")
                raise RuntimeError("CCT driver returned no spectrum")
            wavelength = np.asarray(list(result.Wavelength), dtype=np.float64)
            intensity = np.asarray(list(result.Intensity), dtype=np.float64)
            if wavelength.size != intensity.size or wavelength.size == 0:
                raise RuntimeError("CCT driver returned invalid spectrum arrays")
            triggered = bool(getattr(result, "InputHardwareTrigger", False))
            return Spectrum(wavelength, intensity, triggered=triggered)
        finally:
            with self._cts_lock:
                self._acquire_cts = None
            cts.Dispose()

    def abort(self) -> None:
        """Cancel a pending acquisition (e.g. one waiting for a trigger); safe from any thread."""
        with self._cts_lock:
            if self._acquire_cts is not None:
                self._aborted = True
                self._acquire_cts.Cancel()

    @property
    def _spectrometer(self):
        if self._device is None:
            raise RuntimeError("spectrometer is not connected")
        return self._device

    @staticmethod
    def _check(result: bool, operation: str) -> None:
        if not result:
            raise RuntimeError(f"CCT driver could not {operation}")
