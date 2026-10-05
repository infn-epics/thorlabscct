import numpy as np
import pytest

from thorlabscct10_ioc.backend import SimulatedCCT
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
