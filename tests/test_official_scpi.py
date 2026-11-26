import asyncio

import pytest

from redpitayacore_mcp import official_scpi
from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.official_scpi import OfficialSCPI


async def test_mock_never_opens_socket(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Mock must not open a socket")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    adapter = OfficialSCPI(Settings())
    identity = await adapter.read("identity", False)
    assert identity["simulated"] is True
    assert identity["image_identity_verified"] is False
    status = await adapter.read("acquisition_status", False)
    assert status["decimation"] == 64
    assert status["buffer_full"] is False


async def test_live_requires_operator_and_call_gates(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Gate must run before opening a socket")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    with pytest.raises(PermissionError):
        await OfficialSCPI(Settings(mode="live", host="192.0.2.1")).read("identity", True)
    enabled = Settings(mode="live", host="192.0.2.1", allow_scpi=True)
    with pytest.raises(PermissionError):
        await OfficialSCPI(enabled).read("identity", False)
    with pytest.raises(ValueError):
        Settings(mode="live", host="192.0.2.1", allow_scpi=True, allow_rx=True)


@pytest.mark.parametrize(
    "query, replies, expected",
    [
        ("identity", [b"REDPITAYA,INSTR,MOCK,0", b"3.00-57"], "identity"),
        ("acquisition_status", [b"REDPITAYA,INSTR,MOCK,0", b"128", b"TD", b"1"], "buffer_full"),
    ],
)
async def test_official_wire_queries(monkeypatch, query, replies, expected):
    received = []

    async def fixture(reader, writer):
        try:
            for response in replies:
                received.append(await reader.readuntil(b"\r\n"))
                writer.write(response + b"\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    monkeypatch.setattr(official_scpi, "SCPI_PORT", server.sockets[0].getsockname()[1])
    async with server:
        result = await OfficialSCPI(
            Settings(mode="live", host="127.0.0.1", allow_scpi=True)
        ).read(query, True)
    assert expected in result
    assert result["simulated"] is False
    assert result["image_identity_verified"] is False
    commands = {
        "identity": [b"*IDN?\r\n", b"SYST:VERS?\r\n"],
        "acquisition_status": [
            b"*IDN?\r\n",
            b"ACQ:DEC:Factor?\r\n",
            b"ACQ:TRIG:STAT?\r\n",
            b"ACQ:TRIG:FILL?\r\n",
        ],
    }
    assert received == commands[query]


@pytest.mark.parametrize("response", [b"x" * 2048 + b"\r\n", b"partial", b"\r\n", b"OTHER,INSTR,X,Y\r\n"])
async def test_bad_response_fails_closed(monkeypatch, response):
    async def fixture(reader, writer):
        try:
            await reader.readuntil(b"\r\n")
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    monkeypatch.setattr(official_scpi, "SCPI_PORT", server.sockets[0].getsockname()[1])
    async with server:
        with pytest.raises(ValueError):
            await OfficialSCPI(
                Settings(mode="live", host="127.0.0.1", allow_scpi=True)
            ).read("identity", True)
