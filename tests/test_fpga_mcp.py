import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_fpga_tools_over_real_stdio_are_local_and_hardware_plans_never_execute(tmp_path):
    (tmp_path / "counter.sv").write_text("module counter(input clk); endmodule\n")
    (tmp_path / "candidate.bit").write_bytes(b"fixture, not a real bitstream")
    (tmp_path / "candidate.dtbo").write_bytes(b"fixture, not a real overlay")
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "RP_MODE": "mock",
        "RP_HOST": "",
        "RP_SSH_USER": "",
        "RP_ALLOW_RX": "false",
        "RP_ALLOW_LIFECYCLE": "false",
        "RP_PROJECT_ROOT": str(tmp_path),
        "RP_ALLOW_PROJECT_WRITE": "true",
        "RP_ALLOW_LOCAL_BUILD": "true",
        "RP_VIVADO_EXECUTABLE": "",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "redpitayacore_mcp"], env=env
    )
    async with asyncio.timeout(20), stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert not {
                "rp_fpga_build", "rp_fpga_write_source", "rp_fpga_build_cancel",
                "rp_fpga_build_status", "rp_fpga_build_report", "rp_fpga_ila_plan",
            }.intersection(tools)
            assert tools["rp_fpga_read_source"].annotations.readOnlyHint is True
            caps = await session.call_tool("rp_fpga_capabilities", {})
            assert caps.structuredContent["rtl_source_editing"] is False
            assert caps.structuredContent["toolchain"]["execution_enabled"] is False
            assert tools["rp_fpga_hardware_plan"].annotations.readOnlyHint is True
            info = await session.call_tool("rp_fpga_project_info", {})
            assert info.structuredContent["available"] is True
            source = await session.call_tool("rp_fpga_read_source", {"path": "counter.sv"})
            sha = source.structuredContent["sha256"]
            updated = await session.call_tool(
                "rp_fpga_write_source",
                {
                    "path": "counter.sv",
                    "content": "module counter(input clk); endmodule\n// edited\n",
                    "expected_sha256": sha,
                },
            )
            assert updated.isError
            assert (tmp_path / "counter.sv").read_text() == "module counter(input clk); endmodule\n"
            stale = await session.call_tool(
                "rp_fpga_write_source",
                {"path": "counter.sv", "content": "stale", "expected_sha256": sha},
            )
            assert stale.isError
            escape = await session.call_tool("rp_fpga_read_source", {"path": "../secret.sv"})
            assert escape.isError
            artifact = await session.call_tool(
                "rp_fpga_inspect_artifact", {"path": "candidate.bit"}
            )
            assert artifact.structuredContent["format_verified"] is False
            manifest = await session.call_tool(
                "rp_fpga_manifest",
                {
                    "bitstream_path": "candidate.bit",
                    "dtbo_path": "candidate.dtbo",
                    "part": "xc7z010clg400-1",
                },
            )
            assert not manifest.isError
            assert manifest.structuredContent["approved"] is False
            plan = await session.call_tool(
                "rp_fpga_build",
                {
                    "stage": "synthesis",
                    "top": "counter",
                    "part": "xc7z010clg400-1",
                    "sources": ["counter.sv"],
                },
            )
            assert plan.isError
            denied = await session.call_tool(
                "rp_fpga_build",
                {
                    "stage": "synthesis",
                    "top": "counter",
                    "part": "xc7z010clg400-1",
                    "sources": ["counter.sv"],
                    "dry_run": False,
                },
            )
            assert denied.isError
            hardware = await session.call_tool("rp_fpga_hardware_plan", {"operation": "tx_burst"})
            assert hardware.structuredContent["execution_supported"] is False
            assert hardware.structuredContent["executed"] is False
            assert not (tmp_path / ".redpitayacore-builds").exists()
