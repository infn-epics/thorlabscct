from pathlib import Path

import pytest

from thorlabscct10_ioc.config import ConfigError, parse_config
from thorlabscct10_ioc.ioc import build_arg_parser, main, resolve_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_single_device_from_ioc_chart_dump():
    # shape of /tmp/iocconfig.yaml written by ioc-chart (chart keys are ignored)
    config = parse_config({
        "config_dir": "/epics/ioc/config",
        "ioctop": "/epics/ioc/config",
        "beamline": "sparc",
        "iocname": "cct01",
        "image": "registry.example/thorlabscct:0.2.0",
        "devtype": "thorlabscct",
        "iocprefix": "SPARC:DIAG:CCT01",
        "server": "192.168.0.160",
        "exposure": "12.5",
        "average": 4,
        "shutter": "closed",
        "continuous": "true",
        "period": 0.5,
        "serial": {"ip": None, "port": None},
    })
    assert config.name == "cct01"
    (device,) = config.devices
    assert device.prefix == "SPARC:DIAG:CCT01:"
    assert device.ip == "192.168.0.160"
    assert device.exposure == 12.5 and device.average == 4
    assert device.shutter_open is False and device.continuous is True


def test_chart_serial_mapping_is_not_a_device_id():
    config = parse_config({"iocprefix": "X", "serial": {"ip": None, "port": None, "baud": 9600}})
    assert config.devices[0].device_id is None


def test_devices_inherit_top_level_defaults():
    config = parse_config({
        "iocprefix": "SPARC:DIAG:SPEC:",
        "continuous": True,
        "exposure": 5,
        "devices": [
            {"name": "CCT01", "server": "10.0.0.1"},
            {"name": "CCT02", "ip": "10.0.0.2", "exposure": 20},
            {"name": "CCT03", "disable": True},
            {"name": "x", "prefix": "OTHER:CCT:"},
        ],
    })
    prefixes = [d.prefix for d in config.devices]
    assert prefixes == ["SPARC:DIAG:SPEC:CCT01:", "SPARC:DIAG:SPEC:CCT02:", "OTHER:CCT:"]
    assert [d.exposure for d in config.devices] == [5, 20, 5]
    assert all(d.continuous for d in config.devices)
    assert config.devices[1].ip == "10.0.0.2"


@pytest.mark.parametrize("data", [
    {},
    {"iocprefix": "X", "exposure": 0},
    {"iocprefix": "X", "average": "many"},
    {"iocprefix": "X", "continuous": "maybe"},
    {"iocprefix": "X", "devices": [{"server": "1.2.3.4"}]},
    {"iocprefix": "X", "devices": [{"name": "A"}, {"name": "A"}]},
    {"iocprefix": "X", "devices": {"name": "A"}},
])
def test_invalid_configurations(data):
    with pytest.raises(ConfigError):
        parse_config(data)


def test_cli_overrides_yaml(tmp_path):
    path = tmp_path / "ioc.yaml"
    path.write_text("iocprefix: LAB:CCT\nserver: 1.2.3.4\nexposure: 3\n")
    args = build_arg_parser().parse_args(["--config", str(path), "--exposure", "7", "--simulate"])
    config = resolve_config(args)
    (device,) = config.devices
    assert device.prefix == "LAB:CCT:"
    assert device.exposure == 7.0 and device.simulate is True and device.ip == "1.2.3.4"


def test_cli_without_yaml_keeps_legacy_defaults():
    config = resolve_config(build_arg_parser().parse_args([]))
    (device,) = config.devices
    assert device.prefix == "CCT10:" and device.exposure == 8.3 and not device.simulate


def test_example_lists_pvs_for_every_device(capsys):
    main(["--config", str(EXAMPLES / "simulator.yaml"), "--list-pvs"])
    pvs = capsys.readouterr().out.split()
    assert "LAB:SPEC:CCT01:Spectrum" in pvs
    assert "LAB:SPEC:CCT02:Spectrum" in pvs


def test_epik8s_snippet_instances_parse():
    yaml = pytest.importorskip("yaml")
    values = yaml.safe_load((EXAMPLES / "epik8s" / "values-snippet.yaml").read_text())
    defaults = values["iocDefaults"]["thorlabscct"]
    for ioc in values["epicsConfiguration"]["iocs"].values():
        assert "template" not in {**defaults, **ioc}
        assert parse_config({**defaults, **ioc}).devices


def _pyioc_spec():
    pyioc = pytest.importorskip("epik8s_pyioc")
    return pyioc, pyioc.load_spec(Path(__file__).resolve().parent.parent
                                  / "src" / "thorlabscct10_ioc" / "pyioc.yaml")


def test_pyioc_spec_matches_legacy_config_defaults():
    pyioc, spec = _pyioc_spec()
    instance = pyioc.resolve_instance(spec, {"iocprefix": "X", "server": "1.2.3.4"})
    legacy = parse_config({"iocprefix": "X", "server": "1.2.3.4"}).devices[0]
    (device,) = instance.devices
    assert device.prefix == legacy.prefix and device.server == legacy.ip
    assert (device.exposure, device.average, device.period) == (legacy.exposure, legacy.average, legacy.period)
    assert (device.shutter == "open") is legacy.shutter_open and device.continuous is legacy.continuous


def test_pyioc_validates_the_epik8s_snippet():
    yaml = pytest.importorskip("yaml")
    _, spec = _pyioc_spec()
    from epik8s_pyioc.tools import validate_values

    values = yaml.safe_load((EXAMPLES / "epik8s" / "values-snippet.yaml").read_text())
    checked, errors, warnings = validate_values(spec, values)
    assert checked == 2 and errors == [] and warnings == []


def test_run_pyioc_entrypoint(monkeypatch):
    pyioc, spec = _pyioc_spec()
    from thorlabscct10_ioc import ioc as ioc_module

    captured = {}
    monkeypatch.setattr(ioc_module, "serve", lambda config, list_pvs=False: captured.update(config=config))
    instance = pyioc.resolve_instance(spec, {
        "iocname": "multi", "iocprefix": "SPARC:DIAG:SPEC", "simulate": "true", "shutter": "closed",
        "devices": [{"name": "CCT01", "ip": "10.0.0.1"}, {"name": "CCT02", "exposure": "20"}],
    })
    assert ioc_module.run_pyioc(instance) == 0
    config = captured["config"]
    assert [d.prefix for d in config.devices] == ["SPARC:DIAG:SPEC:CCT01:", "SPARC:DIAG:SPEC:CCT02:"]
    assert config.devices[0].ip == "10.0.0.1" and config.devices[1].exposure == 20.0
    assert all(d.simulate and not d.shutter_open for d in config.devices)


def test_trigger_settings_from_yaml_and_spec():
    config = parse_config({"iocprefix": "X", "trigger_mode": "hardware", "trigger_slope": "Falling",
                           "trigger_averaging": "single_trigger", "trigger_out_delay": "-5",
                           "trigger_timeout": 0})
    (device,) = config.devices
    assert device.trigger_hardware and device.trigger_falling and device.trigger_single
    assert device.trigger_out_delay == -5.0 and device.trigger_timeout == 0.0
    with pytest.raises(ConfigError):
        parse_config({"iocprefix": "X", "trigger_mode": "sometimes"})

    pyioc, spec = _pyioc_spec()
    from thorlabscct10_ioc import ioc as ioc_module

    captured = {}
    import pytest as _pytest  # noqa: F401
    instance = pyioc.resolve_instance(spec, {"iocprefix": "X", "trigger_mode": "hardware",
                                             "trigger_slope": "falling", "sim_trigger_rate": 5})
    original = ioc_module.serve
    ioc_module.serve = lambda config, list_pvs=False: captured.update(config=config)
    try:
        ioc_module.run_pyioc(instance)
    finally:
        ioc_module.serve = original
    (device,) = captured["config"].devices
    assert device.trigger_hardware and device.trigger_falling and not device.trigger_single
    assert device.sim_trigger_rate == 5.0
