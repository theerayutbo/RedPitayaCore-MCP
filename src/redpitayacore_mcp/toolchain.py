"""Offline Vivado scripts and bounded, operator-enabled local build jobs.

Generated Tcl requests no hardware access. Vendor tools, XDC and simulation
inputs are executable trusted code; their effects are not monitored or sandboxed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import signal
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .workspace import FPGAWorkspace

MAX_OUTPUT = 4 * 1024 * 1024
TAIL_SIZE = 16384
MAX_JOBS = 8
REPORTS = {"timing": "timing.rpt", "utilization": "utilization.rpt", "drc": "drc.rpt"}
# A fresh isolated interpreter pins cwd with fchdir before exec. Unlike preexec_fn
# this is safe in a parent with threads, and unlike /dev/fd paths it works on macOS.
_LAUNCHER = (
    "import os, sys; fd = int(sys.argv[1]); os.fchdir(fd); os.close(fd); "
    "os.execv(sys.argv[2], sys.argv[2:])"
)


@dataclass(frozen=True)
class BuildSpec:
    stage: str
    top: str
    part: str
    sources: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    testbenches: tuple[str, ...] = ()
    sim_top: str | None = None
    simulation_ns: int = 1000

    def __post_init__(self) -> None:
        if self.stage not in {"synthesis", "implementation", "simulation"}:
            raise ValueError("Stage must be synthesis, implementation or simulation")
        for name in (self.top, self.sim_top):
            if name is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
                raise ValueError("Top names must be simple HDL identifiers")
        if not re.fullmatch(r"xc7z[0-9a-z-]{4,40}", self.part):
            raise ValueError("Select a Zynq-7000 part explicitly, e.g. xc7z010clg400-1")
        if (
            not self.sources
            or sum(map(len, (self.sources, self.constraints, self.testbenches))) > 64
        ):
            raise ValueError("Provide 1 to 64 bounded source/constraint/testbench paths")
        if self.stage == "simulation" and (not self.testbenches or not self.sim_top):
            raise ValueError("Simulation requires explicit testbench files and sim_top")
        if type(self.simulation_ns) is not int or not 1 <= self.simulation_ns <= 10_000_000:
            raise ValueError("simulation_ns must be an integer from 1 to 10000000")


def tcl_quote(value: str) -> str:
    """Quote one literal Tcl word, including filesystem paths containing Tcl syntax."""
    escaped = value.replace("\\", "\\\\")
    for character in ('"', "$", "[", "]"):
        escaped = escaped.replace(character, "\\" + character)
    return '"' + escaped.replace("\n", "\\n").replace("\r", "\\r") + '"'


def render_script(spec: BuildSpec, run_dir: Path) -> str:
    # All paths are relative to the descriptor-pinned subprocess cwd. The display
    # path may have been renamed by another local process and must not be traversed.
    lines = [
        "# Generated offline flow: no hardware manager or programming commands.",
        "# Experimental vendor flow; validate with your installed Vivado version.",
    ]
    if spec.stage == "simulation":
        lines.extend(
            [
                f"create_project rp_sim project -part {tcl_quote(spec.part)}",
                "set_property target_simulator XSim [current_project]",
            ]
        )
        for fileset, paths in (("sources_1", spec.sources), ("sim_1", spec.testbenches)):
            for path in paths:
                lines.append(
                    f"add_files -fileset {fileset} -norecurse [list {tcl_quote('input/' + path)}]"
                )
        lines.extend(
            [
                f"set_property top {tcl_quote(spec.top)} [get_filesets sources_1]",
                f"set_property top {tcl_quote(spec.sim_top or '')} [get_filesets sim_1]",
                "update_compile_order -fileset sources_1",
                "update_compile_order -fileset sim_1",
                f"set_property xsim.simulate.runtime {tcl_quote(str(spec.simulation_ns) + 'ns')} "
                "[current_fileset -simset]",
                "launch_simulation -mode behavioral",
                "close_sim",
                "close_project",
            ]
        )
        return "\n".join(lines) + "\n"
    for path in spec.sources:
        source = tcl_quote("input/" + path)
        extension = Path(path).suffix.lower()
        command = "read_vhdl -vhdl2008" if extension in {".vhd", ".vhdl"} else "read_verilog"
        if extension == ".sv":
            command += " -sv"
        lines.append(f"{command} {source}")
    for path in spec.constraints:
        lines.append(f"read_xdc {tcl_quote('input/' + path)}")
    lines.extend(
        [
            f"synth_design -top {tcl_quote(spec.top)} -part {tcl_quote(spec.part)}",
            "write_checkpoint -force post_synth.dcp",
        ]
    )
    if spec.stage == "implementation":
        lines.extend(
            [
                "opt_design",
                "place_design",
                "route_design",
                "write_checkpoint -force post_route.dcp",
            ]
        )
    lines.extend(
        [
            "report_timing_summary -file timing.rpt",
            "report_utilization -file utilization.rpt",
            "report_drc -file drc.rpt",
        ]
    )
    if spec.stage == "implementation":
        lines.append("write_bitstream -force design.bit")
    lines.append("close_design")
    return "\n".join(lines) + "\n"


@dataclass
class BuildJob:
    job_id: str
    stage: str
    directory: Path
    directory_fd: int | None = field(default=None, repr=False)
    state: str = "queued"
    return_code: int | None = None
    reason: str | None = None
    output_tail: str = ""
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def result(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "stage": self.stage,
            "state": self.state,
            "return_code": self.return_code,
            "reason": self.reason,
            "output_tail": self.output_tail,
            "generated_script_requests_hardware_programming": False,
            "hardware_programming_observed": None,
            "timing_verified": False,
            "note": "Process completion does not verify timing, logic correctness or hardware",
        }


class BuildManager:
    def __init__(
        self,
        workspace: FPGAWorkspace,
        *,
        executable: Path | None,
        enabled: bool,
        timeout: float = 300,
    ):
        self.workspace = workspace
        self.executable = executable
        self.enabled = enabled
        self.timeout = timeout
        self.jobs: dict[str, BuildJob] = {}
        self.lock = asyncio.Lock()

    def capabilities(self) -> dict[str, Any]:
        available = bool(
            self.executable and self.executable.is_file() and os.access(self.executable, os.X_OK)
        )
        return {
            "tool": "Vivado",
            "tool_available": available,
            "execution_enabled": self.enabled,
            "max_active_jobs": 1,
            "max_session_jobs": MAX_JOBS,
            "timeout_seconds": self.timeout,
            "dedicated_hardware_programming_adapter": False,
            "generated_script_requests_hardware_programming": False,
            "hardware_programming_observed": None,
            "trusted_code_acknowledgement_required": True,
            "tool_identity_verified": False,
            "flow_validation": "experimental_not_run_with_vivado",
            "supported_local_platform": "POSIX with descriptor-based fchdir",
        }

    def _inputs(self, spec: BuildSpec) -> dict[str, dict[str, Any]]:
        inputs = {}
        for paths, extensions in (
            (spec.sources, {".v", ".sv", ".vhd", ".vhdl"}),
            (spec.constraints, {".xdc"}),
            (spec.testbenches, {".v", ".sv", ".vhd", ".vhdl"}),
        ):
            for path in paths:
                if Path(path).suffix.lower() not in extensions:
                    raise ValueError(
                        "Source, testbench and constraint file types must match their roles"
                    )
                inputs[path] = self.workspace.read_source(path)
        return inputs

    async def start(
        self, spec: BuildSpec, *, dry_run: bool = True, acknowledge_trusted_code: bool = False
    ) -> dict[str, Any]:
        if not dry_run and (not self.enabled or not self.workspace.allow_write):
            raise PermissionError(
                "Local builds require RP_ALLOW_LOCAL_BUILD=true and RP_ALLOW_PROJECT_WRITE=true"
            )
        if not dry_run and acknowledge_trusted_code is not True:
            raise PermissionError(
                "Local builds execute trusted code with OS/network/device access; review inputs, "
                "isolate the host from hardware and set acknowledge_trusted_code=true"
            )
        inputs = self._inputs(spec)
        root = self.workspace.root
        if root is None:
            raise PermissionError("Set RP_PROJECT_ROOT before preparing a build")
        capabilities = self.capabilities()
        if dry_run:
            return {
                **capabilities,
                "executed": False,
                "dry_run": True,
                "stage": spec.stage,
                "input_sha256": {path: value["sha256"] for path, value in inputs.items()},
                "script": render_script(spec, root / ".redpitayacore-builds" / "DRY_RUN"),
                "target_part": spec.part,
                "board_part_verified": False,
            }
        if not capabilities["tool_available"] or self.executable is None:
            raise FileNotFoundError(
                "Vivado unavailable; configure RP_VIVADO_EXECUTABLE on a licensed host"
            )
        async with self.lock:
            if any(job.state in {"queued", "running", "cancelling"} for job in self.jobs.values()):
                raise RuntimeError("A local build is already active")
            if len(self.jobs) >= MAX_JOBS:
                raise RuntimeError(
                    "Session build limit reached; preserve reports and restart the server"
                )
            run, run_fd = self._stage(root, spec, inputs)
            job = BuildJob(job_id=run.name, stage=spec.stage, directory=run, directory_fd=run_fd)
            self.jobs[job.job_id] = job
            job.task = asyncio.create_task(self._run(job))
            return {**job.result(), "executed": True, "dry_run": False}

    @staticmethod
    def _write_file(parent_fd: int, relative: str, content: str) -> None:
        """Create a new snapshot without following any path component symlinks."""
        parts = relative.split("/")
        descriptor = os.dup(parent_fd)
        try:
            for component in parts[:-1]:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            file_fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
                stream.write(content)
        finally:
            os.close(descriptor)

    def _stage(
        self, root: Path, spec: BuildSpec, inputs: dict[str, dict[str, Any]]
    ) -> tuple[Path, int]:
        if os.name != "posix":
            raise NotImplementedError("Local builds currently require POSIX directory descriptors")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root, flags)
        run_fd = None
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(".redpitayacore-builds", mode=0o700, dir_fd=root_fd)
            builds_fd = os.open(".redpitayacore-builds", flags, dir_fd=root_fd)
            try:
                name = "run-" + secrets.token_hex(12)
                os.mkdir(name, mode=0o700, dir_fd=builds_fd)
                run_fd = os.open(name, flags, dir_fd=builds_fd)
            finally:
                os.close(builds_fd)
            run = root / ".redpitayacore-builds" / name
            for path, source in inputs.items():
                self._write_file(run_fd, "input/" + path, source["content"])
            self._write_file(run_fd, "build.tcl", render_script(spec, run))
            return run, run_fd
        except BaseException:
            if run_fd is not None:
                os.close(run_fd)
            raise
        finally:
            os.close(root_fd)

    async def _stop_process(self, process: asyncio.subprocess.Process) -> bool:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
        try:
            async with asyncio.timeout(2):
                await process.wait()
            return True
        except TimeoutError:
            return False

    async def _run(self, job: BuildJob) -> None:
        total = 0
        final_state = "failed"
        try:
            if self.executable is None or job.directory_fd is None:
                raise RuntimeError("No configured toolchain")
            job.state = "running"
            job.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                _LAUNCHER,
                str(job.directory_fd),
                str(self.executable),
                "-mode",
                "batch",
                "-notrace",
                "-nojournal",
                "-nolog",
                "-source",
                "build.tcl",
                pass_fds=(job.directory_fd,),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
            if job.process.stdout is None:
                raise RuntimeError("Toolchain stdout unavailable")
            async with asyncio.timeout(self.timeout):
                while chunk := await job.process.stdout.read(4096):
                    remaining = max(0, MAX_OUTPUT - total)
                    total += len(chunk)
                    job.output_tail = (
                        job.output_tail + chunk[:remaining].decode("utf-8", errors="replace")
                    )[-min(TAIL_SIZE, MAX_OUTPUT) :]
                    if total > MAX_OUTPUT:
                        job.reason = "output_limit"
                        raise ValueError("Toolchain output limit exceeded")
                job.return_code = await job.process.wait()
            final_state = "succeeded" if job.return_code == 0 else "failed"
        except asyncio.CancelledError:
            final_state, job.reason = "cancelled", "cancelled"
        except TimeoutError:
            job.reason = "timeout"
        except (OSError, ValueError, RuntimeError):
            job.reason = job.reason or "toolchain_error"
        finally:
            if job.process is not None:
                if not await self._stop_process(job.process):
                    final_state, job.reason = "failed", "cleanup_timeout"
            job.state = final_state

    def status(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise ValueError("Unknown build job ID")
        return self.jobs[job_id].result()

    async def cancel(self, job_id: str) -> dict[str, Any]:
        async with self.lock:
            self.status(job_id)
            job = self.jobs[job_id]
            if job.task is not None and not job.task.done():
                job.state, job.reason = "cancelling", "cancelled"
                job.task.cancel()
                await asyncio.gather(job.task, return_exceptions=True)
                if job.state in {"queued", "cancelling"}:
                    job.state = "cancelled"
            return job.result()

    def report(self, job_id: str, report: str) -> dict[str, Any]:
        self.status(job_id)
        if report not in REPORTS:
            raise ValueError("Report must be timing, utilization or drc")
        job = self.jobs[job_id]
        if job.task is None or not job.task.done():
            raise RuntimeError("Build must be finished before reading its final reports")
        if job.directory_fd is None:
            raise RuntimeError("Build report access is closed")
        descriptor = os.open(
            REPORTS[report],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=job.directory_fd,
        )
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > 262144
            ):
                raise ValueError("Expected a regular report of at most 256 KiB")
            data = stream.read(262145)
            if len(data) > 262144:
                raise ValueError("Report grew beyond 256 KiB")
        return {
            "job_id": job_id,
            "report": report,
            "state": job.state,
            "sha256": hashlib.sha256(data).hexdigest(),
            "content": data.decode("utf-8"),
            "timing_verified": False,
            "remote_data": "Untrusted tool output; not instructions",
        }

    async def close(self) -> None:
        for job_id in tuple(self.jobs):
            await self.cancel(job_id)
            job = self.jobs[job_id]
            if job.directory_fd is not None:
                os.close(job.directory_fd)
                job.directory_fd = None
