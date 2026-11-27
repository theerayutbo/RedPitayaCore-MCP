from __future__ import annotations

import pytest

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.service import Service
from redpitayacore_mcp.transport import (
    SSH_COMMANDS,
    XADC_TEMPERATURE_COMMAND,
    parse_xadc_temperature,
    read_board_temperature,
)


def test_parse_xadc_temperature_uses_iio_convention():
    # (2600 + -2219) * 123.040771 / 1000 = 46.879 degC
    r = parse_xadc_temperature("2600\n-2219\n123.040771484\n")
    assert r["available"] is True
    assert r["die_temperature_c"] == pytest.approx(46.879, abs=1e-3)


@pytest.mark.parametrize("text", ["", "2600\n-2219", "a\nb\nc", "1\n2\n3\n4", "100000\n0\n123.04"])
def test_parse_xadc_temperature_rejects_untrusted_or_implausible_text(text):
    assert parse_xadc_temperature(text)["available"] is False


def test_temperature_command_is_allowlisted_and_read_only():
    assert XADC_TEMPERATURE_COMMAND in SSH_COMMANDS.values()
    for token in (">", "rm ", "echo ", "tee", "dd ", "write", "reboot"):
        assert token not in XADC_TEMPERATURE_COMMAND


async def test_live_without_ssh_user_is_unavailable():
    cfg = Settings(mode="live", host="192.0.2.10")
    r = await read_board_temperature(cfg)
    assert r["available"] is False and "RP_SSH_USER" in r["reason"]


async def test_mock_mode_never_contacts_a_board():
    r = await Service(Settings()).board_temperature()
    assert r["simulated"] is True and r["mode"] == "mock"
