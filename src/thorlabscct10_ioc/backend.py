"""Hardware and simulation backends for Thorlabs CCT spectrometers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import os
import sys
import time
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Spectrum:
    wavelength: np.ndarray
    intensity: np.ndarray


class CCTBackend(Protocol):
    device_id: str

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def set_exposure(self, milliseconds: float) -> None: ...
    def set_average(self, frames: int) -> None: ...
    def set_shutter(self, opened: bool) -> None: ...
    def acquire_dark(self) -> None: ...
    def acquire(self) -> Spectrum: ...


class SimulatedCCT:
    """Deterministic CCT10-like simulator, useful without EPICS or vendor software."""

    device_id = "SIM:CCT10"

    def __init__(self, pixels: int = 2048, seed: int = 10):
        self.pixels = pixels
        self.exposure_ms = 8.3
        self.average = 1
        self.shutter_open = True
        self.connected = False
        self._rng = np.random.default_rng(seed)
        self._dark = np.zeros(pixels)

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

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

    def acquire(self) -> Spectrum:
        self._require_connected()
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
        return Spectrum(wavelength, intensity)

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
            self._helper.RegisterEthernetIpAddress(self.ip_address)
        self._helper.WithVirtual = self.virtual
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

    def acquire_dark(self) -> None:
        restore_open = self._shutter_open
        self.set_shutter(False)
        try:
            self._check(self._spectrometer.UpdateDarkSpectrumAsync(False, self._token).Result,
                        "acquire dark spectrum")
        finally:
            self.set_shutter(restore_open)

    def acquire(self) -> Spectrum:
        result = self._spectrometer.AcquireSingleSpectrumAsync(self._token).Result
        wavelength = np.asarray(list(result.Wavelength), dtype=np.float64)
        intensity = np.asarray(list(result.Intensity), dtype=np.float64)
        if wavelength.size != intensity.size or wavelength.size == 0:
            raise RuntimeError("CCT driver returned invalid spectrum arrays")
        return Spectrum(wavelength, intensity)

    @property
    def _spectrometer(self):
        if self._device is None:
            raise RuntimeError("spectrometer is not connected")
        return self._device

    @staticmethod
    def _check(result: bool, operation: str) -> None:
        if not result:
            raise RuntimeError(f"CCT driver could not {operation}")
