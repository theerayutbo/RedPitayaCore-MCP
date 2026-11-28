"""Offline tests only; hardware behavior remains to be qualified."""
import numpy as np
import pytest

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.scpi_instrument import FS, N, capture, parse_samples, plan


def test_plan_band():
    steps = plan([100, 100_000, 1_000_000, 13_560_000, 20_000_000])
    for s in steps:
        assert s.sample_rate_hz / s.frequency_hz >= 6
        assert N * s.frequency_hz / s.sample_rate_hz >= 16
        assert s.cycles > 8192 * s.frequency_hz * s.decimation / FS
        assert s.cycles < 65536
    with pytest.raises(ValueError):
        plan([0])
    with pytest.raises(ValueError):
        plan([100, 100])
    with pytest.raises(ValueError):
        plan([20_000_001])


def test_mock():
    s = plan([1_000_000])[0]
    a, b, m = capture(
        Settings(), port=5000, decimation=s.decimation, units="VOLTS",
        gain1="LV", gain2="LV", step=s, amplitude_vpk=.01,
    )
    assert a.shape == b.shape == (N,)
    assert m["mode"] == "mock"
    assert np.max(abs(a)) < np.max(abs(b))


def test_parser():
    assert parse_samples("{" + ",".join(["1"] * N) + "}", "RAW").shape == (N,)
    with pytest.raises(ValueError):
        parse_samples("{1,2}", "RAW")
    with pytest.raises(ValueError):
        parse_samples("{" + ",".join(["nan"] * N) + "}", "VOLTS")
    with pytest.raises(ValueError):
        parse_samples("{" + ",".join(["1.1"] * N) + "}", "RAW")


def test_live_refused_before_connection():
    with pytest.raises(PermissionError):
        capture(
            Settings(mode="live", host="192.0.2.1"), port=5000,
            decimation=1, units="VOLTS", gain1="LV", gain2="LV",
        )
