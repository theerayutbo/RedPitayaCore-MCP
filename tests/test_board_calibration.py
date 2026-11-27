from __future__ import annotations

import pytest

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.transport import (
    CALIBRATION_COMMAND,
    SSH_COMMANDS,
    parse_calibration,
    read_board_calibration,
)

# Synthetic values in the real `calib -rv` layout (dataStructureId 1); not from any real board.
SAMPLE = """dataStructureId = 1
wpCheck = 120
FE_CH1_FS_G_HI = 50200000
FE_CH2_FS_G_HI = 50200001
FE_CH1_FS_G_LO = 909000000
FE_CH2_FS_G_LO = 909000001
FE_CH1_DC_offs = -40
FE_CH2_DC_offs = -100
BE_CH1_FS = 46600000
BE_CH2_FS = 46600001
BE_CH1_DC_offs = 60
BE_CH2_DC_offs = 40
Magic = -1430532899
FE_CH1_DC_offs_HI = -50
FE_CH2_DC_offs_HI = -130
"""


def test_parse_calibration_layout():
    r = parse_calibration(SAMPLE)
    assert r["available"] is True and r["data_structure_id"] == 1
    assert r["values"]["FE_CH2_DC_offs_HI"] == -130 and len(r["values"]) == 15


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "FE = 1\nFE = 2",
        "FE_CH1 = 1.5",
        "serial = ab:cd",
        "rm -rf = 1",
        "\n".join(f"K{i} = {i}" for i in range(200)),
    ],
)
def test_parse_calibration_rejects_untrusted_text(bad):
    assert parse_calibration(bad)["available"] is False


def test_calibration_command_is_read_mode_only():
    assert CALIBRATION_COMMAND in SSH_COMMANDS.values()
    assert CALIBRATION_COMMAND == "/opt/redpitaya/bin/calib -rv"
    for flag in (" -w", " -d", " -i", " -o", "|", ";", ">"):
        assert flag not in CALIBRATION_COMMAND


async def test_live_without_ssh_user_is_unavailable():
    r = await read_board_calibration(Settings(mode="live", host="192.0.2.10"))
    assert r["available"] is False
