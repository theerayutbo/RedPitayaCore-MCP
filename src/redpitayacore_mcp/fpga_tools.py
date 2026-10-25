"""MCP surface for local FPGA development and non-executing hardware plans."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Settings
from .fpga_profile import REVISION, hardware_plan, ila_plan, register_map, register_plan
from .service import Service
from .toolchain import BuildManager, BuildSpec
from .workspace import FPGAWorkspace


class FPGAService:
    def __init__(self, config: Settings, sdr: Service):
        self.config = config
        self.sdr = sdr
        self.workspace = FPGAWorkspace(config.project_root, config.allow_project_write)
        self.builds = BuildManager(
            self.workspace,
            executable=config.vivado_executable,
            enabled=config.allow_local_build,
            timeout=config.build_timeout,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "local_project": self.workspace.info(),
            "toolchain": self.builds.capabilities(),
            "rtl_source_editing": self.config.allow_project_write,
            "bitstream_inspection": "local hash and size only; format/target not verified",
            "live_mmio": False,
            "live_fpga_programming": False,
            "live_tx": False,
            "note": "Local project/build permissions are independent of hardware mock mode",
        }

    def manifest(
        self, bitstream_path: str, dtbo_path: str, part: str, source_revision: str = REVISION
    ) -> dict[str, Any]:
        if Path(bitstream_path).suffix not in {".bit", ".bin"}:
            raise ValueError("Bitstream candidate must use .bit or .bin extension")
        if Path(dtbo_path).suffix != ".dtbo":
            raise ValueError("Overlay candidate must use .dtbo extension")
        if not re.fullmatch(r"xc7z[0-9a-z-]{4,40}", part):
            raise ValueError("Select an explicit Zynq-7000 target part")
        if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise ValueError("source_revision must be a full lowercase Git commit ID")
        candidates = {
            "schema": "redpitayacore.image-manifest.v1",
            "board_model": "STEMlab 125-14",
            "part": part,
            "source_revision_claimed": source_revision,
            "source_revision_verified": False,
            "bitstream": self.workspace.inspect_artifact(bitstream_path),
            "dtbo": self.workspace.inspect_artifact(dtbo_path),
        }
        manifest_id = hashlib.sha256(
            json.dumps(candidates, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "manifest_id": manifest_id,
            "manifest": candidates,
            "approved": False,
            "signature_verified": False,
            "board_compatibility_verified": False,
            "note": "A digest identifies this local proposal; it is not approval, a signature "
            "or proof that the source revision produced these files",
        }

    async def diagnostics(self) -> dict[str, Any]:
        result = await self.sdr.system_diagnostics()
        return {
            **result,
            "registers_read": False,
            "fpga_image_verified": False,
            "hardware_tx_state": "unknown_not_read",
            "note": "Read-only OS/manager/overlay/installed-file evidence only. Installed file "
            "hashes and labels cannot establish the active FPGA image.",
        }


def register_fpga_tools(mcp: FastMCP[Any], service: FPGAService) -> None:
    local = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    read_device = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    )
    changes = ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
    build_code = ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
    )

    @mcp.tool(annotations=local)
    def rp_fpga_capabilities() -> dict[str, Any]:
        """Report local FPGA workspace/toolchain availability and explicit hardware limits."""
        return service.capabilities()

    @mcp.tool(annotations=read_device)
    async def rp_fpga_diagnostics() -> dict[str, Any]:
        """Read fixed optional SSH FPGA/overlay/installed-hash evidence. No registers or writes."""
        return await service.diagnostics()

    @mcp.tool(annotations=local)
    def rp_fpga_register_map() -> dict[str, Any]:
        """Describe the pinned SDR instance-1 MMIO map; never access physical memory."""
        return register_map()

    @mcp.tool(annotations=local)
    def rp_fpga_register_plan(
        name: str,
        operation: Literal["read", "write"],
        value: Annotated[int | None, Field(strict=True)] = None,
        expected_value: Annotated[int | None, Field(strict=True)] = None,
    ) -> dict[str, Any]:
        """Validate a named, width-limited register proposal. No actual MMIO access or approval."""
        return register_plan(name, operation, value, expected_value)

    @mcp.tool(annotations=local)
    def rp_fpga_hardware_plan(
        operation: Literal["load_bitstream", "register_write", "tx_burst"],
    ) -> dict[str, Any]:
        """Explain exact blockers/interlocks for image load, register write or TX. Never execute."""
        return hardware_plan(operation)

    @mcp.tool(annotations=local)
    def rp_fpga_project_info() -> dict[str, Any]:
        """List bounded HDL/constraint/artifact paths in the operator-selected local project."""
        return service.workspace.info()

    @mcp.tool(annotations=local)
    def rp_fpga_read_source(path: str) -> dict[str, Any]:
        """Read bounded UTF-8 RTL/XDC and SHA-256. Path is relative to RP_PROJECT_ROOT."""
        return service.workspace.read_source(path)

    @mcp.tool(annotations=changes)
    def rp_fpga_write_source(
        path: str,
        content: Annotated[str, Field(max_length=262144)],
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Write local RTL/XDC with operator permission and current hash for replacements."""
        return service.workspace.write_source(path, content, expected_sha256)

    @mcp.tool(annotations=local)
    def rp_fpga_inspect_artifact(path: str, expected_sha256: str | None = None) -> dict[str, Any]:
        """Hash a local bitstream/DTBO/debug/report/checkpoint candidate; no format/board proof."""
        return service.workspace.inspect_artifact(path, expected_sha256)

    @mcp.tool(annotations=local)
    def rp_fpga_read_report(path: str) -> dict[str, Any]:
        """Read bounded local .rpt/.ltx text as untrusted output; no timing pass claim."""
        return service.workspace.read_report(path)

    @mcp.tool(annotations=local)
    def rp_fpga_manifest(
        bitstream_path: str, dtbo_path: str, part: str, source_revision: str = REVISION
    ) -> dict[str, Any]:
        """Bind local bitstream/DTBO hashes and claimed part/source into an UNAPPROVED proposal."""
        return service.manifest(bitstream_path, dtbo_path, part, source_revision)

    @mcp.tool(annotations=build_code)
    async def rp_fpga_build(
        stage: Literal["synthesis", "implementation", "simulation"],
        top: str,
        part: str,
        sources: Annotated[list[str], Field(min_length=1, max_length=64)],
        constraints: Annotated[list[str], Field(max_length=64)] | None = None,
        testbenches: Annotated[list[str], Field(max_length=64)] | None = None,
        sim_top: str | None = None,
        simulation_ns: Annotated[int, Field(strict=True, ge=1, le=10000000)] = 1000,
        dry_run: Annotated[bool, Field(strict=True)] = True,
        acknowledge_trusted_code: Annotated[bool, Field(strict=True)] = False,
    ) -> dict[str, Any]:
        """Plan or launch a trusted local Vivado job. Default dry run.

        Generated Tcl requests no hardware programming; vendor/input effects are unmonitored.
        Execution needs RP_PROJECT_ROOT, RP_ALLOW_PROJECT_WRITE, RP_ALLOW_LOCAL_BUILD and
        RP_VIVADO_EXECUTABLE plus acknowledge_trusted_code=true on a licensed isolated host.
        XDC/simulation/vendor code can access OS, network and devices; effects are not sandboxed
        or monitored. Review all code and isolate the host from board network/USB/JTAG access.
        """
        spec = BuildSpec(
            stage,
            top,
            part,
            tuple(sources),
            tuple(constraints or ()),
            tuple(testbenches or ()),
            sim_top,
            simulation_ns,
        )
        return await service.builds.start(
            spec, dry_run=dry_run, acknowledge_trusted_code=acknowledge_trusted_code
        )

    @mcp.tool(annotations=local)
    def rp_fpga_build_status(job_id: str) -> dict[str, Any]:
        """Read bounded output/status for an owned local build job; does not verify timing."""
        return service.builds.status(job_id)

    @mcp.tool(annotations=changes)
    async def rp_fpga_build_cancel(job_id: str) -> dict[str, Any]:
        """Cancel one owned local build and terminate its process group."""
        return await service.builds.cancel(job_id)

    @mcp.tool(annotations=local)
    def rp_fpga_build_report(
        job_id: str, report: Literal["timing", "utilization", "drc"]
    ) -> dict[str, Any]:
        """Read a bounded known report from an owned job; text is untrusted diagnostic data."""
        return service.builds.report(job_id, report)

    @mcp.tool(annotations=local)
    def rp_fpga_ila_plan(
        clock_net: str,
        probes: Annotated[list[list[str]], Field(min_length=1, max_length=8)],
        depth: Annotated[int, Field(strict=True)] = 1024,
    ) -> dict[str, Any]:
        """Generate offline ILA Tcl for exact bit nets; no live insertion or capture."""
        return ila_plan(clock_net, probes, depth)
