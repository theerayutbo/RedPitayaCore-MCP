import math
import struct

import pytest

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.protocol import decode_iq, rx_frequency, rx_rate, rx_role


def test_mock_default_has_no_target_or_hardware_permissions():
    config = Settings.from_env({})
    assert config.mode == "mock"
    assert config.host is None
    assert not config.allow_rx
    assert not config.allow_lifecycle
    assert config.ssh_user is None


@pytest.mark.parametrize(
    "env",
    [
        {"RP_MODE": "live"},
        {"RP_MODE": "bogus"},
        {"RP_MODE": "live", "RP_HOST": "example.org"},
        {"RP_HOST": "127.0.0.1/path"},
        {"RP_HOST": "0.0.0.0"},
        {"RP_HOST": "224.0.0.1"},
        {"RP_HOST": "255.255.255.255"},
        {"RP_PORT": "0"},
        {"RP_TIMEOUT": "nan"},
        {"RP_TIMEOUT": "inf"},
        {"RP_TIMEOUT": "0"},
        {"RP_ALLOW_RX": "yes please"},
        {"RP_SSH_USER": "root; id"},
        {"RP_SSH_USER": "-oProxyCommand=bad"},
    ],
)
def test_invalid_config_fails_closed(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


def test_explicit_live_config():
    config = Settings.from_env(
        {"RP_MODE": "live", "RP_HOST": "192.0.2.25", "RP_ALLOW_RX": "true", "RP_SSH_USER": "root"}
    )
    assert config.host == "192.0.2.25"
    assert config.port == 1001
    assert config.allow_rx


def test_verified_wire_vectors():
    assert rx_role("control") == b"\x00\x00\x00\x00"
    assert rx_role("data") == b"\x01\x00\x00\x00"
    assert rx_frequency(7_100_000) == struct.pack("<I", 7_100_000)
    assert rx_rate(50_000) == b"\x01\x00\x00\x10"
    assert [
        struct.unpack("<I", rx_rate(rate))[0]
        for rate in [20_000, 50_000, 100_000, 250_000, 500_000, 1_250_000]
    ] == list(range(0x10000000, 0x10000006))


@pytest.mark.parametrize("role", [2, 3, "tx", "TX", "2", None])
def test_no_tx_role_encoder(role):
    with pytest.raises(ValueError):
        rx_role(role)


@pytest.mark.parametrize("freq", [-1, 0, 60_000_001, 1.5, True])
def test_invalid_frequency(freq):
    with pytest.raises(ValueError):
        rx_frequency(freq)


def test_invalid_rate():
    with pytest.raises(ValueError):
        rx_rate(48_000)


def test_iq_format_and_bad_samples():
    assert decode_iq(struct.pack("<4f", 1, -2, 0.5, 0.25)) == [(1, -2), (0.5, 0.25)]
    with pytest.raises(ValueError):
        decode_iq(b"123")
    with pytest.raises(ValueError):
        decode_iq(struct.pack("<2f", math.nan, 1))
