import asyncio
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

from redpitayacore_mcp.toolchain import BuildManager, BuildSpec, render_script
from redpitayacore_mcp.workspace import FPGAWorkspace


def project(tmp_path):
    (tmp_path / "counter.sv").write_text("module counter(input clk); endmodule\n")
    (tmp_path / "clock.xdc").write_text("create_clock -period 10 [get_ports clk]\n")
    return FPGAWorkspace(tmp_path, allow_write=True)


def spec(stage="synthesis"):
    return BuildSpec(
        stage=stage,
        top="counter",
        part="xc7z010clg400-1",
        sources=("counter.sv",),
        constraints=("clock.xdc",),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stage": "program_device"},
        {"top": "counter; exec bad"},
        {"part": "part$[exec bad]"},
        {"sources": ()},
        {"simulation_ns": True},
        {"simulation_ns": 0},
        {"simulation_ns": 10_000_001},
        {"stage": "simulation", "testbenches": (), "sim_top": None},
    ],
)
def test_bad_build_specs(kwargs):
    values = {
        "stage": "synthesis",
        "top": "counter",
        "part": "xc7z010clg400-1",
        "sources": ("counter.sv",),
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        BuildSpec(**values)


def test_generated_tcl_has_only_offline_build_operations(tmp_path):
    text = render_script(spec("implementation"), tmp_path)
    assert "synth_design" in text and "route_design" in text and "write_bitstream" in text
    assert "report_timing_summary" in text and "report_drc" in text
    assert not any(
        command in text
        for command in ("open_hw_manager", "program_hw_devices", "fpgautil", "exec ")
    )
    assert "create_project" not in text and "close_project" not in text
    assert "close_design" in text


def test_simulation_uses_project_filesets_and_one_explicit_runtime(tmp_path):
    simulation = BuildSpec(
        stage="simulation",
        top="counter",
        part="xc7z010clg400-1",
        sources=("counter.sv",),
        testbenches=("tb_counter.sv",),
        sim_top="tb_counter",
        simulation_ns=250,
    )
    text = render_script(simulation, tmp_path)
    assert "create_project" in text and "close_project" in text
    assert "add_files -fileset sources_1" in text
    assert "add_files -fileset sim_1" in text
    assert 'xsim.simulate.runtime "250ns"' in text
    assert "read_verilog" not in text and "synth_design" not in text
    assert "\nrun " not in text


def test_tcl_syntax_and_path_quoting_with_real_interpreter(tmp_path):
    tclsh = shutil.which("tclsh")
    if not tclsh:
        pytest.skip("tclsh unavailable; Vivado has not been simulated as a real toolchain")
    odd_spec = BuildSpec(
        stage="synthesis",
        top="counter",
        part="xc7z010clg400-1",
        sources=('dir$[error INJECTED];"/counter.sv',),
    )
    text = render_script(odd_spec, tmp_path)
    # Unknown Vivado commands are recorded, not evaluated by a shell.
    stub = "proc unknown {args} { return {} }\n" + text
    result = subprocess.run([tclsh], input=stub, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert not result.stderr


async def test_dry_run_and_disabled_build_do_not_create_files(tmp_path):
    manager = BuildManager(project(tmp_path), executable=None, enabled=False)
    before = set(tmp_path.iterdir())
    planned = await manager.start(spec(), dry_run=True)
    assert planned["executed"] is False
    assert planned["tool_available"] is False
    assert "script" in planned
    with pytest.raises(PermissionError):
        await manager.start(spec(), dry_run=False, acknowledge_trusted_code=True)
    assert set(tmp_path.iterdir()) == before


def fake_tool(tmp_path, body):
    path = tmp_path / "fake-vivado"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("acknowledgement", [False, None, "true", 1])
async def test_enabled_build_requires_exact_trusted_code_acknowledgement(tmp_path, acknowledgement):
    executable = fake_tool(tmp_path, "raise RuntimeError('must not execute')\n")
    manager = BuildManager(project(tmp_path), executable=executable, enabled=True)
    with pytest.raises(PermissionError, match="acknowledge_trusted_code"):
        await manager.start(spec(), dry_run=False, acknowledge_trusted_code=acknowledgement)
    assert not manager.jobs
    assert not (tmp_path / ".redpitayacore-builds").exists()


async def wait_finished(manager, job_id):
    async with asyncio.timeout(5):
        await manager.jobs[job_id].task
    return manager.status(job_id)


async def test_real_subprocess_job_snapshots_inputs_and_keeps_bounded_reports(
    tmp_path, monkeypatch
):
    signals = []
    original_killpg = os.killpg

    def record_signal(pid, sig):
        signals.append((pid, sig))
        original_killpg(pid, sig)

    monkeypatch.setattr(os, "killpg", record_signal)
    executable = fake_tool(
        tmp_path,
        "from pathlib import Path\nprint('fixture only')\n"
        "Path('timing.rpt').write_text('TEST FIXTURE; not timing closure')\n",
    )
    manager = BuildManager(project(tmp_path), executable=executable, enabled=True)
    started = await manager.start(spec(), dry_run=False, acknowledge_trusted_code=True)
    finished = await wait_finished(manager, started["job_id"])
    assert finished["state"] == "succeeded"
    assert finished["generated_script_requests_hardware_programming"] is False
    assert finished["hardware_programming_observed"] is None
    assert finished["timing_verified"] is False
    assert "fixture only" in finished["output_tail"]
    report = manager.report(started["job_id"], "timing")
    assert "TEST FIXTURE" in report["content"]
    assert report["state"] == "succeeded"
    assert report["sha256"] == hashlib.sha256(report["content"].encode()).hexdigest()
    assert not signals, (
        "A completed process group must not be signalled after its leader was reaped"
    )
    await manager.close()


async def test_active_report_is_rejected_instead_of_presented_as_final(tmp_path):
    executable = fake_tool(tmp_path, "import time\ntime.sleep(30)\n")
    manager = BuildManager(project(tmp_path), executable=executable, enabled=True)
    started = await manager.start(spec(), dry_run=False, acknowledge_trusted_code=True)
    (manager.jobs[started["job_id"]].directory / "timing.rpt").write_text("partial")
    with pytest.raises(RuntimeError, match="finished"):
        manager.report(started["job_id"], "timing")
    await manager.close()


async def test_build_staging_process_and_reports_survive_directory_symlink_swap(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    executable = fake_tool(
        tmp_path,
        "from pathlib import Path\n"
        "assert 'synth_design' in Path('build.tcl').read_text()\n"
        "Path('timing.rpt').write_text(Path('input/counter.sv').read_text())\n",
    )
    manager = BuildManager(project(root), executable=executable, enabled=True)
    original_mkdir = os.mkdir
    swapped = False

    def swap_before_run_directory(path, *args, **kwargs):
        nonlocal swapped
        if str(path).rsplit("/", 1)[-1].startswith("run-") and not swapped:
            swapped = True
            (root / ".redpitayacore-builds").rename(root / "previous-builds")
            (root / ".redpitayacore-builds").symlink_to(outside, target_is_directory=True)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", swap_before_run_directory)
    started = await manager.start(spec(), dry_run=False, acknowledge_trusted_code=True)
    finished = await wait_finished(manager, started["job_id"])
    assert swapped
    assert not list(outside.iterdir()), "Build files escaped through the replaced directory"
    assert finished["state"] == "succeeded"
    report = manager.report(started["job_id"], "timing")
    assert "module counter" in report["content"]
    await manager.close()


async def test_job_cancellation_and_single_active_limit(tmp_path):
    executable = fake_tool(tmp_path, "import time\nprint('running', flush=True)\ntime.sleep(30)\n")
    manager = BuildManager(project(tmp_path), executable=executable, enabled=True, timeout=5)
    started = await manager.start(spec(), dry_run=False, acknowledge_trusted_code=True)
    with pytest.raises(RuntimeError, match="active"):
        await manager.start(spec(), dry_run=False, acknowledge_trusted_code=True)
    await asyncio.sleep(0.05)
    cancelled = await manager.cancel(started["job_id"])
    assert cancelled["state"] == "cancelled"
    await manager.close()


async def test_timeout_and_oversized_output_fail_closed(tmp_path, monkeypatch):
    executable = fake_tool(tmp_path, "import time\nprint('x'*10000, flush=True)\ntime.sleep(30)\n")
    monkeypatch.setattr("redpitayacore_mcp.toolchain.MAX_OUTPUT", 100)
    manager = BuildManager(project(tmp_path), executable=executable, enabled=True, timeout=0.1)
    started = await manager.start(spec(), dry_run=False, acknowledge_trusted_code=True)
    finished = await wait_finished(manager, started["job_id"])
    assert finished["state"] == "failed"
    assert finished["reason"] in {"output_limit", "timeout"}
    assert len(finished["output_tail"]) <= 100
    await manager.close()
