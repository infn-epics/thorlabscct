"""YAML instance configuration (epik8s / ioc-chart style).

The IOC can be instantiated from the same per-IOC mapping that is written in an
epik8s beamline ``values.yaml`` under ``epicsConfiguration.iocs``.  ioc-chart
dumps that mapping (merged with ``iocDefaults``) into the pod as
``/tmp/iocconfig.yaml``; this module turns it into one or more device
definitions.  Unknown keys (chart/deployment settings) are ignored.

Single spectrometer::

    iocprefix: SPARC:DIAG:CCT01
    server: 192.168.0.160
    exposure: 10

Several spectrometers served by one IOC process (PVs: ``<iocprefix>:<name>:...``)::

    iocprefix: SPARC:DIAG
    devices:
      - name: CCT01
        server: 192.168.0.160
      - name: CCT02
        server: 192.168.0.161
        average: 4

Keys given at top level act as defaults for every entry of ``devices``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Mapping

DEFAULT_SDK_PATH = "/opt/thorlabs/cct"

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


class ConfigError(ValueError):
    """Raised for an invalid YAML instance configuration."""


@dataclass
class DeviceConfig:
    prefix: str
    name: str = ""
    ip: str | None = None
    device_id: str | None = None
    virtual: bool = False
    simulate: bool = False
    sdk_path: str = DEFAULT_SDK_PATH
    exposure: float = 8.3
    average: int = 1
    shutter_open: bool = True
    continuous: bool = False
    period: float = 1.0
    trigger_hardware: bool = False
    trigger_falling: bool = False
    trigger_single: bool = False
    trigger_out_delay: float = 0.0
    trigger_timeout: float = 10.0
    sim_trigger_rate: float = 0.0

    def validate(self) -> None:
        where = f"device {self.name!r}" if self.name else "device"
        if not self.prefix:
            raise ConfigError(f"{where}: empty PV prefix")
        if not 0.01 <= self.exposure <= 30000.0:
            raise ConfigError(f"{where}: exposure must be in the range 0.01 .. 30000 ms")
        if not 1 <= self.average <= 10000:
            raise ConfigError(f"{where}: average must be in the range 1 .. 10000")
        if not 0.03 <= self.period <= 3600.0:
            raise ConfigError(f"{where}: period must be in the range 0.03 .. 3600 s")
        if not -60000.0 <= self.trigger_out_delay <= 60000.0:
            raise ConfigError(f"{where}: trigger_out_delay must be in the range -60000 .. 60000 ms")
        if self.trigger_timeout < 0:
            raise ConfigError(f"{where}: trigger_timeout must be >= 0 s (0 = wait forever)")
        if self.sim_trigger_rate < 0:
            raise ConfigError(f"{where}: sim_trigger_rate must be >= 0 Hz")


@dataclass
class IOCConfig:
    devices: list[DeviceConfig]
    interfaces: list[str] = field(default_factory=lambda: ["0.0.0.0"])
    log_level: str = "INFO"
    log_pv_names: bool = False
    name: str = ""


def as_bool(value: Any, key: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{key}: cannot interpret {value!r} as a boolean")


def _shutter(value: Any) -> bool:
    if isinstance(value, str) and value.strip().lower() in ("open", "opened"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("close", "closed"):
        return False
    return as_bool(value, "shutter")


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def _join_prefix(base: str, name: str) -> str:
    base = str(base or "")
    if name:
        base = f"{base.rstrip(':')}:{name}" if base else str(name)
    if base and not base.endswith(":"):
        base += ":"
    return base


def _number(value: Any, kind: type, key: str):
    try:
        return kind(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: invalid value {value!r}") from exc


def _choice(mapping: Mapping[str, Any], key: str, choices: dict[str, bool], default: bool) -> bool:
    value = mapping.get(key)
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text not in choices:
        raise ConfigError(f"{key}: {value!r} is not one of {', '.join(choices)}")
    return choices[text]


def _device(merged: Mapping[str, Any], base_prefix: str, name: str) -> DeviceConfig:
    device_id = _first(merged, "device_id")
    device = DeviceConfig(
        prefix=str(_first(merged, "prefix", default="")) or _join_prefix(base_prefix, name),
        name=name,
        ip=_first(merged, "server", "ip"),
        device_id=str(device_id) if device_id is not None else None,
        virtual=as_bool(merged.get("virtual", False), "virtual"),
        simulate=as_bool(merged.get("simulate", False), "simulate"),
        sdk_path=str(_first(merged, "sdk_path", default=os.environ.get("SDK_PATH", DEFAULT_SDK_PATH))),
        exposure=_number(_first(merged, "exposure", "exposure_ms", default=8.3), float, "exposure"),
        average=_number(_first(merged, "average", default=1), int, "average"),
        shutter_open=_shutter(_first(merged, "shutter", default="open")),
        continuous=as_bool(merged.get("continuous", False), "continuous"),
        period=_number(_first(merged, "period", default=1.0), float, "period"),
        trigger_hardware=_choice(merged, "trigger_mode", {"free": False, "hardware": True}, False),
        trigger_falling=_choice(merged, "trigger_slope", {"rising": False, "falling": True}, False),
        trigger_single=_choice(merged, "trigger_averaging",
                               {"per_frame": False, "single_trigger": True}, False),
        trigger_out_delay=_number(_first(merged, "trigger_out_delay", default=0.0), float,
                                  "trigger_out_delay"),
        trigger_timeout=_number(_first(merged, "trigger_timeout", default=10.0), float,
                                "trigger_timeout"),
        sim_trigger_rate=_number(_first(merged, "sim_trigger_rate", default=0.0), float,
                                 "sim_trigger_rate"),
    )
    if device.ip is not None:
        device.ip = str(device.ip)
    device.validate()
    return device


# keys that describe one spectrometer and are inherited by ``devices`` entries
_DEVICE_KEYS = (
    "server", "ip", "device_id", "virtual", "simulate", "sdk_path",
    "exposure", "exposure_ms", "average", "shutter", "continuous", "period",
    "trigger_mode", "trigger_slope", "trigger_averaging", "trigger_out_delay", "trigger_timeout",
    "sim_trigger_rate",
)


def parse_config(data: Mapping[str, Any]) -> IOCConfig:
    if not isinstance(data, Mapping):
        raise ConfigError("configuration must be a YAML mapping")
    base_prefix = _first(data, "iocprefix", "prefix")
    if not base_prefix:
        raise ConfigError("'iocprefix' is required")
    base_prefix = str(base_prefix)
    defaults = {key: data[key] for key in _DEVICE_KEYS if key in data}

    entries = data.get("devices") or []
    if not isinstance(entries, list):
        raise ConfigError("'devices' must be a list")
    devices: list[DeviceConfig] = []
    if entries:
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ConfigError(f"devices[{index}] must be a mapping")
            if as_bool(entry.get("disable", False), "disable"):
                continue
            name = str(entry.get("name") or "")
            if not name and not entry.get("prefix"):
                raise ConfigError(f"devices[{index}] needs a 'name' (or explicit 'prefix')")
            devices.append(_device({**defaults, **entry}, base_prefix, name))
        if not devices:
            raise ConfigError("all devices are disabled")
    else:
        devices.append(_device({**defaults, "prefix": None}, base_prefix, ""))

    prefixes = [device.prefix for device in devices]
    duplicates = sorted({p for p in prefixes if prefixes.count(p) > 1})
    if duplicates:
        raise ConfigError(f"duplicate PV prefixes: {', '.join(duplicates)}")

    interfaces = _first(data, "interfaces", default="0.0.0.0")
    if isinstance(interfaces, str):
        interfaces = [item.strip() for item in interfaces.split(",") if item.strip()]
    log_level = str(_first(data, "log_level", "loglevel", default="INFO")).upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"invalid log_level {log_level!r}")
    return IOCConfig(
        devices=devices,
        interfaces=[str(item) for item in interfaces] or ["0.0.0.0"],
        log_level=log_level,
        log_pv_names=as_bool(data.get("log_pv_names", False), "log_pv_names"),
        name=str(_first(data, "iocname", "name", default="")),
    )


def load_config(path: str | os.PathLike[str]) -> IOCConfig:
    import yaml

    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return parse_config(data or {})
