import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.service import Service


async def test_mock_workflow_never_uses_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Mock must not touch sockets or subprocesses")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    service = Service(Settings())
    assert service.profile()["mode"] == "mock"
    assert service.profile()["mcp_tx_path_present"] is False
    assert service.profile()["hardware_tx_state"] == "unknown_not_read"
    assert "tx_enabled" not in service.status()
    assert (await service.diagnostics())["simulated"]
    assert (await service.probe())["simulated"]
    assert (await service.system_diagnostics())["simulated"]
    await service.connect(7_100_000, 50_000, False)
    result = await service.read(2048, 8)
    assert result["sample_count"] == 2048
    assert result["mean_power"] == pytest.approx(0.25)
    assert result["settings_verified"] is False
    assert len(result["preview_iq"]) == 8
    assert (await service.disconnect())["session_open"] is False


async def test_live_rx_two_gates_and_lifecycle_never_touch_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Safety guards must run before network access")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    config = Settings(mode="live", host="192.0.2.25", allow_lifecycle=True)
    service = Service(config)
    with pytest.raises(PermissionError, match="RP_ALLOW_RX"):
        await service.connect(600_000, 100_000, True)
    service2 = Service(Settings(mode="live", host="192.0.2.25", allow_rx=True))
    with pytest.raises(PermissionError, match="acknowledge_side_effects"):
        await service2.connect(600_000, 100_000, False)
    for action in ["start", "stop"]:
        plan = await service.lifecycle(action, True)
        assert plan["executed"] is False
        assert plan["live_allowed"] is False
        with pytest.raises(PermissionError):
            await service.lifecycle(action, False)


async def test_mock_lifecycle_and_invalid_inputs():
    service = Service(Settings(allow_lifecycle=True))
    assert (await service.lifecycle("stop", False))["app_running"] is False
    with pytest.raises(ConnectionError):
        await service.connect(600_000, 100_000, False)
    assert (await service.lifecycle("start", False))["app_running"] is True
    with pytest.raises(ValueError):
        await service.lifecycle("anything", False)
    with pytest.raises(ValueError):
        await service.connect(-1, 50_000, False)
    await service.connect(600_000, 100_000, False)
    with pytest.raises(ValueError):
        await service.connect(600_000, 100_000, False)
    for count in [0, 8193, True, 1.5]:
        with pytest.raises(ValueError):
            await service.read(count, 8)
    with pytest.raises(ValueError):
        await service.read(1, 33)
    await service.disconnect()
    with pytest.raises(ConnectionError):
        await service.read(1, 0)


async def test_actual_mcp_stdio_initialize_list_call_and_validation():
    env = {**os.environ, "RP_MODE": "mock", "PYTHONPATH": "src"}
    # Do not inherit the operator's optional live permission flags.
    env["RP_ALLOW_RX"] = "false"
    env["RP_ALLOW_LIFECYCLE"] = "false"
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "redpitayacore_mcp"], env=env
    )
    async with asyncio.timeout(20), stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "redpitaya_core_mcp"
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {
                "rp_profile",
                "rp_device_diagnostics",
                "rp_system_diagnostics",
                "rp_connection_probe",
                "rp_connection_status",
                "rp_app_lifecycle",
                "rp_rx_connect",
                "rp_rx_read",
                "rp_rx_disconnect",
            }
            assert not any("tx" in name for name in names)
            annotations = {tool.name: tool.annotations for tool in listed.tools}
            assert annotations["rp_rx_connect"].readOnlyHint is False
            assert annotations["rp_rx_read"].readOnlyHint is False
            assert annotations["rp_device_diagnostics"].readOnlyHint is True
            profile = await session.call_tool("rp_profile", {})
            assert not profile.isError
            assert profile.structuredContent["mode"] == "mock"
            opened = await session.call_tool("rp_rx_connect", {})
            assert not opened.isError
            result = await session.call_tool("rp_rx_read", {"sample_count": 16})
            assert result.structuredContent["sample_count"] == 16
            bad = await session.call_tool("rp_rx_read", {"sample_count": 8193})
            assert bad.isError
            bad_type = await session.call_tool("rp_rx_connect", {"frequency_hz": True})
            assert bad_type.isError
            unknown = await session.call_tool("rp_tx_enable", {})
            assert unknown.isError
            resources = await session.list_resources()
            assert any(
                str(resource.uri) == "redpitaya://safety" for resource in resources.resources
            )
            safety = await session.read_resource("redpitaya://safety")
            assert "TX" in json.dumps(safety.model_dump(mode="json"))


async def test_concurrent_system_diagnostics_are_bounded(monkeypatch):
    active, peak = 0, 0

    async def fake_read(config):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"available": False}

    monkeypatch.setattr("redpitayacore_mcp.service.read_system", fake_read)
    service = Service(Settings(mode="live", host="192.0.2.25", ssh_user="root"))
    await asyncio.gather(*(service.system_diagnostics() for _ in range(5)))
    assert peak == 1
