from __future__ import annotations

import pytest

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.service import Service
from redpitayacore_mcp.transport import (
    BOARD_HEALTH_COMMAND,
    SSH_COMMANDS,
    parse_board_health,
    read_board_health,
)

# Values read from a STEMlab 125-14 (OS 2.x) on 2026-11-27, identifiers removed.
SAMPLE = """temp_raw=2767
temp_offset=-2219
temp_scale=123.040771484
vccint_raw=1379
vccint_scale=0.732421875
vccaux_raw=2471
vccaux_scale=0.732421875
vccoddr_raw=2053
vccoddr_scale=0.732421875
uptime_s=36398.60
loadavg=0.08 0.02 0.01
eth0_rx_dropped=0
eth0_rx_errors=0
"""


def test_parse_board_health_sample():
    r = parse_board_health(SAMPLE)
    assert r["available"] is True
    assert r["die_temperature_c"] == pytest.approx(67.426, abs=1e-3)
    assert r["supply_rails"]["vccint"]["volts"] == pytest.approx(1.0100, abs=1e-4)
    assert r["supply_rails"]["vccaux"]["in_nominal_range"] is True
    assert r["supply_rails"]["vccoddr"]["volts"] == pytest.approx(1.5037, abs=1e-4)
    assert r["uptime_s"] == pytest.approx(36398.6) and r["loadavg"] == [0.08, 0.02, 0.01]
    assert r["eth0_counters"] == {"eth0_rx_dropped": 0, "eth0_rx_errors": 0}


@pytest.mark.parametrize(
    "bad",
    [
        "mac=00:11:22:33:44:55",  # unknown key (identifiers must never pass)
        "temp_raw=1\ntemp_raw=2",  # duplicate key
        "temp_raw=abc\ntemp_offset=0\ntemp_scale=1",  # non-numeric
        "garbage line",  # no key=value
        "",  # nothing found
    ],
)
def test_parse_board_health_rejects_untrusted_text(bad):
    assert parse_board_health(bad)["available"] is False


def test_implausible_temperature_is_flagged_not_reported():
    text = "temp_raw=100000\ntemp_offset=0\ntemp_scale=123.04\nvccint_raw=1379\nvccint_scale=0.73"
    r = parse_board_health(text)
    assert r["die_temperature_c"] is None and "die_temperature_note" in r


def test_health_command_is_allowlisted_and_read_only():
    assert BOARD_HEALTH_COMMAND in SSH_COMMANDS.values()
    for token in (">", "rm ", "tee", "dd ", "write", "reboot", "address", "dna", "/dev/mem"):
        assert token not in BOARD_HEALTH_COMMAND


async def test_live_without_ssh_user_is_unavailable():
    r = await read_board_health(Settings(mode="live", host="192.0.2.10"))
    assert r["available"] is False and "RP_SSH_USER" in r["reason"]


async def test_mock_mode_never_contacts_a_board():
    r = await Service(Settings()).board_health()
    assert r["simulated"] is True and r["mode"] == "mock"
