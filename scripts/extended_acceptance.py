#!/usr/bin/env python3
"""Acceptance beyond live_acceptance.py: every tool Vivado does not need. Needs numpy.

Usage: python scripts/extended_acceptance.py [--report FILE]
       [--pinned-source sdr-transceiver.c]
       [--host BOARD_IPV4 --i-own-this-idle-board --ssh-user USER [--stream-seconds 20]]

Without --host no device is contacted. With it, the board must be idle: the script starts and
stops sdr_transceiver with the board's own API, stops it mid-session on purpose, and opens only
RX roles. It never keys TX. --ssh-user also copies the installed image files off the board with
scp, to compare their hashes. rp_fpga_build is only exercised up to its dry run and refusals.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from live_acceptance import RESULTS, analyse, app, capture, check
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from redpitayacore_mcp import __version__
from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.protocol import RATES
from redpitayacore_mcp.service import Service
from redpitayacore_mcp.transport import probe_tcp, read_system

REPO = Path(__file__).resolve().parents[1]
APP_DIR = "/opt/redpitaya/www/apps/sdr_transceiver"
APP_START_S = 4
# Non-integer period, so a dropped run of samples cannot land on a whole number of cycles.
TONE_RATIO = 0.0973
PHASE_JUMP_RAD = 0.1
PHASE_WINDOW = 64
# Above this the board and link keep up, but the Python decode path may not.
MAX_SUSTAINED_RATE = 100_000
DEMO_BUILD = {
    "stage": "synthesis",
    "top": "simple_counter",
    "part": "xc7z010clg400-1",
    "sources": ["simple_counter.sv"],
}
C_NAMES = {
    "rx_reset": "rx_rst",
    "tx_reset": "tx_rst",
    "gpio_ptt": "gpio",
    "rx_phase_increment": "rx_freq",
    "rx_sync": "rx_sync",
    "rx_rate_divisor": "rx_rate",
    "tx_phase_increment": "tx_freq",
    "tx_sync": "tx_sync",
    "tx_rate_divisor": "tx_rate",
    "rx_fifo_count": "rx_cntr",
    "tx_fifo_count": "tx_cntr",
}


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        **os.environ,
        "RP_MODE": "mock",
        "RP_HOST": "",
        "RP_ALLOW_RX": "false",
        "RP_ALLOW_LIFECYCLE": "false",
        "RP_SSH_USER": "",
        "RP_PROJECT_ROOT": "",
        "RP_ALLOW_PROJECT_WRITE": "false",
        "RP_ALLOW_LOCAL_BUILD": "false",
        "RP_VIVADO_EXECUTABLE": "",
    }
    env.update(overrides)
    return env


@contextlib.asynccontextmanager
async def session(**env: str) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "redpitayacore_mcp"], env=base_env(**env)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def data(client: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.isError:
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structuredContent or {}


async def refused(client: ClientSession, tool: str, **arguments: Any) -> bool:
    return (await client.call_tool(tool, arguments)).isError


async def plans_and_profile(pinned_source: Path | None) -> None:
    async with session() as client:
        profile = await data(client, "rp_profile")
        check("profile: version", profile["server_version"] == __version__, __version__)
        check(
            "profile: no TX path",
            profile["tx_supported"] is False and profile["mcp_tx_path_present"] is False,
        )
        caps = await data(client, "rp_fpga_capabilities")
        check(
            "capabilities: no live MMIO, FPGA load or TX",
            not (caps["live_mmio"] or caps["live_fpga_programming"] or caps["live_tx"]),
        )
        check("capabilities: no project by default", caps["local_project"]["available"] is False)
        check("capabilities: build off", caps["toolchain"]["execution_enabled"] is False)

        registers = (await data(client, "rp_fpga_register_map"))["registers"]
        check("register map: 11 named registers", len(registers) == 11)
        if pinned_source is not None:
            source = await asyncio.to_thread(pinned_source.read_text)
            base = {"cfg": 0x40000000, "sts": 0x41000000}
            typed = {
                name: (int(width), base[region] + int(offset))
                for name, width, region, offset in re.findall(
                    r"(\w+) = \(uint(\d+)_t \*\)\((cfg|sts) \+ (\d+)\)", source
                )
            }
            for item in registers:
                width, address = typed[C_NAMES[item["name"]]]
                check(
                    f"register map matches pinned source: {item['name']}",
                    item["width_bits"] == width and int(item["address"], 16) == address,
                    item["address"],
                )

        plan = await data(
            client, "rp_fpga_register_plan", name="rx_rate_divisor", operation="write", value=500
        )
        check("register plan: valid write stays a plan", plan["executed"] is False)
        plan = await data(client, "rp_fpga_register_plan", name="rx_fifo_count", operation="read")
        check("register plan: valid read stays a plan", plan["execution_supported"] is False)
        for label, arguments in {
            "unknown name": {"name": "nope", "operation": "read"},
            "wrong direction": {"name": "rx_fifo_count", "operation": "write", "value": 1},
            "too wide": {"name": "gpio_ptt", "operation": "write", "value": 256},
            "write without value": {"name": "gpio_ptt", "operation": "write"},
            "bad operation": {"name": "gpio_ptt", "operation": "toggle"},
        }.items():
            check(
                f"register plan refuses {label}",
                await refused(client, "rp_fpga_register_plan", **arguments),
            )

        for operation in ("load_bitstream", "register_write", "tx_burst"):
            plan = await data(client, "rp_fpga_hardware_plan", operation=operation)
            check(
                f"hardware plan: {operation} not executable",
                plan["executed"] is False and plan["execution_supported"] is False,
                len(plan["blockers"]),
            )
        check(
            "hardware plan refuses unknown operation",
            await refused(client, "rp_fpga_hardware_plan", operation="erase"),
        )

        ila = await data(
            client,
            "rp_fpga_ila_plan",
            clock_net="clk",
            probes=[["a[0]", "a[1]"], ["b"]],
            depth=2048,
        )
        stub = "proc unknown {args} {return net}\n" + ila["script"]
        tcl = await asyncio.to_thread(
            subprocess.run, ["tclsh"], input=stub, capture_output=True, text=True, timeout=10
        )
        check("ILA plan: real Tcl parses it, vendor commands stubbed", tcl.returncode == 0)
        check("ILA plan: no capture", ila["hardware_capture_supported"] is False)
        for label, arguments in {
            "Tcl injection": {"clock_net": "clk;exec rm", "probes": [["a"]]},
            "bracket command": {"clock_net": "clk", "probes": [["[exec id]"]]},
            "wildcard": {"clock_net": "clk*", "probes": [["a"]]},
            "bad depth": {"clock_net": "clk", "probes": [["a"]], "depth": 1000},
            "empty probe": {"clock_net": "clk", "probes": [[]]},
        }.items():
            check(
                f"ILA plan refuses {label}", await refused(client, "rp_fpga_ila_plan", **arguments)
            )

        check("no TX tool exists", await refused(client, "rp_tx_enable"))
        tools = (await client.list_tools()).tools
        check(
            "25 tools, none named for TX",
            len(tools) == 25 and not any("tx" in t.name for t in tools),
        )


def fill_project(root: Path) -> None:
    for name in ("simple_counter.sv", "tb_counter.sv", "clock.xdc"):
        shutil.copy(REPO / "examples/fpga-demo" / name, root / name)
    (root / "build.rpt").write_text("Slack (MET) : 1.0ns\n")
    (root / "binary.rpt").write_bytes(b"\xff\xfe\x00bad")
    (root / "big.sv").write_text("x" * (256 * 1024 + 1))
    (root / "notes.txt").write_text("not a project file")
    (root.parent / "secret.sv").write_text("outside")
    (root / "linked.sv").symlink_to(root.parent / "secret.sv")
    (root / "hard_source.sv").write_text("two names")
    os.link(root / "hard_source.sv", root / "hard_copy.sv")
    (root / "image.bit").write_bytes(b"fixture")
    (root / "image.dtbo").write_bytes(b"fixture")


async def project_tools() -> None:
    with tempfile.TemporaryDirectory() as parent:
        root = Path(parent) / "project"
        root.mkdir()
        fill_project(root)
        async with session(RP_PROJECT_ROOT=str(root)) as client:
            info = await data(client, "rp_fpga_project_info")
            check("project: lists the real demo", "simple_counter.sv" in info["files"])
            check("project: omits other file types", "notes.txt" not in info["files"])
            check("project: omits symlink", "linked.sv" not in info["files"])
            check("project: omits hard link", "hard_source.sv" not in info["files"])
            source = await data(client, "rp_fpga_read_source", path="simple_counter.sv")
            check("read source: real demo", "module" in source["content"], source["sha256"][:12])
            check(
                "write refused unless enabled",
                await refused(client, "rp_fpga_write_source", path="new.sv", content="x"),
            )
            check(
                "build refused unless enabled",
                await refused(client, "rp_fpga_build", **DEMO_BUILD, dry_run=False),
            )
            for label, path in {
                "traversal": "../secret.sv",
                "nested traversal": "a/../../secret.sv",
                "absolute": "/etc/hosts",
                "hidden": ".git/x.sv",
                "wrong extension": "notes.txt",
                "backslash": "..\\secret.sv",
                "NUL": "a\x00.sv",
                "symlink": "linked.sv",
                "hard link": "hard_copy.sv",
                "oversize": "big.sv",
                "missing": "absent.sv",
            }.items():
                check(
                    f"read refuses {label}", await refused(client, "rp_fpga_read_source", path=path)
                )

            report = await data(client, "rp_fpga_read_report", path="build.rpt")
            check("read report: text returned", "Slack" in report["content"])
            check(
                "read report refuses non-UTF-8",
                await refused(client, "rp_fpga_read_report", path="binary.rpt"),
            )
            check(
                "read report refuses a source path",
                await refused(client, "rp_fpga_read_report", path="simple_counter.sv"),
            )
            artifact = await data(client, "rp_fpga_inspect_artifact", path="image.bit")
            digest = artifact["sha256"]
            check(
                "inspect: claims nothing about format or board",
                artifact["format_verified"] is False
                and artifact["board_compatibility_verified"] is False
                and artifact["fpga_loaded"] is False,
            )
            match = await data(
                client, "rp_fpga_inspect_artifact", path="image.bit", expected_sha256=digest
            )
            check("inspect: correct hash matches", match["hash_matches"] is True)
            wrong = await data(
                client, "rp_fpga_inspect_artifact", path="image.bit", expected_sha256="0" * 64
            )
            check("inspect: wrong hash does not match", wrong["hash_matches"] is False)
            check(
                "inspect refuses malformed hash",
                await refused(
                    client, "rp_fpga_inspect_artifact", path="image.bit", expected_sha256="abc"
                ),
            )

            base = {"bitstream_path": "image.bit", "dtbo_path": "image.dtbo"}
            manifest = await data(client, "rp_fpga_manifest", **base, part="xc7z010clg400-1")
            again = await data(client, "rp_fpga_manifest", **base, part="xc7z010clg400-1")
            check("manifest: stays unapproved", manifest["approved"] is False)
            check("manifest: digest stable", manifest["manifest_id"] == again["manifest_id"])
            check(
                "manifest refuses non-Zynq part",
                await refused(client, "rp_fpga_manifest", **base, part="xc7a35t"),
            )
            check(
                "manifest refuses wrong overlay type",
                await refused(
                    client,
                    "rp_fpga_manifest",
                    bitstream_path="image.bit",
                    dtbo_path="image.bit",
                    part="xc7z010clg400-1",
                ),
            )
            for stage, extra in {
                "synthesis": {"constraints": ["clock.xdc"]},
                "implementation": {"constraints": ["clock.xdc"]},
                "simulation": {"testbenches": ["tb_counter.sv"], "sim_top": "tb_counter"},
            }.items():
                plan = await data(
                    client, "rp_fpga_build", **{**DEMO_BUILD, "stage": stage}, **extra
                )
                check(f"build dry run: {stage} does not execute", plan["executed"] is False)
            check(
                "build dry run refuses path escape",
                await refused(
                    client, "rp_fpga_build", **{**DEMO_BUILD, "sources": ["../secret.sv"]}
                ),
            )
            check(
                "build refuses a Tcl-shaped top name",
                await refused(client, "rp_fpga_build", **{**DEMO_BUILD, "top": "a;exec id"}),
            )
            check(
                "no build directory after dry runs",
                not (root / ".redpitayacore-builds").exists(),
            )
            for tool, arguments in {
                "rp_fpga_build_status": {},
                "rp_fpga_build_cancel": {},
                "rp_fpga_build_report": {"report": "timing"},
            }.items():
                check(
                    f"{tool} refuses unknown job",
                    await refused(client, tool, job_id="missing", **arguments),
                )

        async with session(RP_PROJECT_ROOT=str(root), RP_ALLOW_PROJECT_WRITE="true") as client:
            created = await data(
                client, "rp_fpga_write_source", path="rtl/new.sv", content="// a\n"
            )
            check("write: creates in a new directory", created["created"] is True)
            check(
                "write refuses replacing without a hash",
                await refused(client, "rp_fpga_write_source", path="rtl/new.sv", content="b"),
            )
            replaced = await data(
                client,
                "rp_fpga_write_source",
                path="rtl/new.sv",
                content="// b\n",
                expected_sha256=created["sha256"],
            )
            check("write: replaces with current hash", replaced["created"] is False)
            check(
                "write refuses stale hash",
                await refused(
                    client,
                    "rp_fpga_write_source",
                    path="rtl/new.sv",
                    content="c",
                    expected_sha256=created["sha256"],
                ),
            )
            check(
                "write refuses through a symlink",
                await refused(
                    client,
                    "rp_fpga_write_source",
                    path="linked.sv",
                    content="x",
                    expected_sha256="0" * 64,
                ),
            )
            check(
                "write left the outside file alone",
                (root.parent / "secret.sv").read_text() == "outside",
            )
            leftovers = [item.name for item in (root / "rtl").iterdir() if item.name != "new.sv"]
            check("write leaves no temp files", not leftovers, leftovers)


def phase_jumps(samples: np.ndarray, frequency: int, rate: int) -> tuple[int, float]:
    """Count lasting phase shifts. Averaging ignores noise, which steps away and straight back."""
    index = np.arange(samples.size)
    mixed = samples * np.exp(2j * np.pi * frequency * index / rate)
    usable = mixed.size - mixed.size % PHASE_WINDOW
    means = mixed[:usable].reshape(-1, PHASE_WINDOW).mean(axis=1)
    step = np.angle(means[1:] * np.conj(means[:-1]))
    residual = np.abs(step - np.median(step))
    return int(np.count_nonzero(residual > PHASE_JUMP_RAD)), float(residual.max())


async def stream(service: Service, frequency: int, rate: int, seconds: float) -> dict[str, float]:
    """Read in one session for seconds; count phase jumps of the ADC-offset tone."""
    await service.connect(frequency, rate, True)
    chunks: list[np.ndarray] = []
    try:
        await asyncio.sleep(0.2)
        start = time.monotonic()
        count = 0
        marks: list[tuple[float, int]] = []
        while (now := time.monotonic()) - start < seconds:
            pairs = np.array(await service.rx.read(8192), dtype=np.float32)
            chunks.append(pairs[:, 0] + 1j * pairs[:, 1])
            count += 8192
            marks.append((now, count))
    finally:
        await service.disconnect()
    # The first second holds whatever queued in socket buffers before reading began.
    settled = next((mark for mark in marks if mark[0] - start >= 1), marks[0])
    measured = (marks[-1][1] - settled[1]) / max(marks[-1][0] - settled[0], 1e-9)
    samples = np.concatenate(chunks)[2048:]
    jumps, worst = phase_jumps(samples, frequency, rate)
    return {
        "samples": float(samples.size),
        "seconds": float(marks[-1][0] - start),
        "rate": float(measured),
        "jumps": float(jumps),
        "worst_rad": worst,
    }


async def continuity(config: Settings, seconds: float) -> None:
    for rate in RATES:
        if rate > MAX_SUSTAINED_RATE:
            frequency = round(rate * TONE_RATIO)
            samples = (await capture(config, frequency, rate))[2048:]
            jumps, worst = phase_jumps(samples, frequency, rate)
            # Not a pass condition: continuity above this rate depends on host load.
            print(f"MEASURE  snapshot {rate} S/s: {jumps} gaps, worst {worst:.3f} rad", flush=True)
            damaged = np.delete(samples, samples.size // 2)
            caught, shift = phase_jumps(damaged, frequency, rate)
            check(
                f"control: one deleted sample is caught, {rate} S/s", caught > 0, f"{shift:.3f} rad"
            )
            continue
        result = await stream(Service(config), round(rate * TONE_RATIO), rate, seconds)
        check(
            f"stream: {rate} S/s kept real time for {seconds:.0f} s",
            abs(result["rate"] / rate - 1) < 0.02,
            f"{result['rate']:.0f} S/s, {result['samples']:.0f} samples",
        )
        check(
            f"stream: {rate} S/s no sample gaps",
            result["jumps"] == 0,
            f"{result['jumps']:.0f} jumps, worst {result['worst_rad']:.3f} rad",
        )


async def diagnostics_during_stream(config: Settings, ssh_user: str, seconds: float) -> None:
    rate = 100_000
    other = Service(replace(config, ssh_user=ssh_user or None))
    stop = asyncio.Event()
    calls = 0

    async def poll() -> None:
        nonlocal calls
        while not stop.is_set():
            await other.diagnostics()
            await other.probe()
            if ssh_user:
                await other.system_diagnostics()
            calls += 1
            await asyncio.sleep(0.5)

    poller = asyncio.create_task(poll())
    try:
        result = await stream(Service(config), round(rate * TONE_RATIO), rate, seconds)
    finally:
        stop.set()
        await poller
    check(
        "stream unbroken while diagnostics repeat",
        result["jumps"] == 0 and abs(result["rate"] / rate - 1) < 0.02,
        f"{calls} rounds, {result['jumps']:.0f} jumps, {result['rate']:.0f} S/s",
    )


async def tuning(config: Settings) -> None:
    for rate, frequencies in {
        1_250_000: (1_000, 10_000, 100_000, 300_000, 600_000),
        20_000: (100, 1_000, 9_000),
    }.items():
        for frequency in frequencies:
            samples = await capture(config, frequency, rate)
            tone, spur = analyse(samples, frequency, rate)
            tolerance = max(2 * rate / samples.size, abs(frequency) * 1e-4)
            check(
                f"tuning: {frequency} Hz at {rate} S/s",
                abs(tone + frequency) <= tolerance and spur < -60,
                f"{tone:.2f} Hz, {spur:.1f} dBc",
            )


async def limits(host: str) -> None:
    env = {"RP_MODE": "live", "RP_HOST": host, "RP_ALLOW_RX": "true", "RP_TIMEOUT": "5"}
    async with session(**env) as client:
        for frequency, rate in ((1, 20_000), (60_000_000, 1_250_000)):
            await data(
                client,
                "rp_rx_connect",
                frequency_hz=frequency,
                sample_rate=rate,
                acknowledge_side_effects=True,
            )
            read = await data(client, "rp_rx_read", sample_count=2048, preview_samples=32)
            check(
                f"limits: {frequency} Hz at {rate} S/s reads",
                read["sample_count"] == 2048 and len(read["preview_iq"]) == 32,
                read["rms_magnitude"],
            )
            await data(client, "rp_rx_disconnect")
        for label, tool, arguments in (
            ("frequency 0", "rp_rx_connect", {"frequency_hz": 0}),
            ("frequency 60000001", "rp_rx_connect", {"frequency_hz": 60_000_001}),
            ("rate 12345", "rp_rx_connect", {"sample_rate": 12_345}),
            ("boolean frequency", "rp_rx_connect", {"frequency_hz": True}),
            ("read with no session", "rp_rx_read", {}),
        ):
            check(
                f"limits: refuses {label}",
                await refused(client, tool, acknowledge_side_effects=True, **arguments)
                if tool == "rp_rx_connect"
                else await refused(client, tool, **arguments),
            )
        await data(client, "rp_rx_connect", acknowledge_side_effects=True)
        for label, arguments in {
            "0 samples": {"sample_count": 0},
            "8193 samples": {"sample_count": 8193},
            "33 previews": {"preview_samples": 33},
        }.items():
            check(f"limits: refuses {label}", await refused(client, "rp_rx_read", **arguments))
        after = await data(client, "rp_rx_read", sample_count=16)
        check("limits: session survives refused calls", after["sample_count"] == 16)
        await data(client, "rp_rx_disconnect")
    async with session(**env, RP_RX_SAMPLE_ORDER="raw") as client:
        check(
            "raw order: profile says so",
            (await data(client, "rp_profile"))["rx_sample_order"] == "raw",
        )
        await data(client, "rp_rx_connect", acknowledge_side_effects=True)
        read = await data(client, "rp_rx_read", sample_count=16)
        check("raw order: read says so", read["sample_order"] == "raw")
        await data(client, "rp_rx_disconnect")


async def churn(config: Settings, cycles: int) -> None:
    service = Service(config)
    failures = 0
    start = time.monotonic()
    for cycle in range(cycles):
        try:
            await service.connect(600_000 + cycle, RATES[cycle % len(RATES)], True)
            await service.read(64, 0)
        except (ConnectionError, OSError, TimeoutError):
            failures += 1
        await service.disconnect()
    check(
        f"churn: {cycles} connect, read, disconnect cycles",
        failures == 0,
        f"{failures} failed, {time.monotonic() - start:.1f} s",
    )


async def client_dies(host: str, config: Settings) -> None:
    code = (
        "import asyncio, os\n"
        "from redpitayacore_mcp.config import Settings\n"
        "from redpitayacore_mcp.service import Service\n"
        "async def main():\n"
        f"    s = Service(Settings(mode='live', host={host!r}, allow_rx=True, timeout=5))\n"
        "    await s.connect(600000, 100000, True)\n"
        "    await s.read(2048, 0)\n"
        "    os._exit(9)\n"
        "asyncio.run(main())\n"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code)
    check("killed client exited mid-session", await process.wait() == 9)
    await asyncio.sleep(0.5)
    service = Service(config)
    await service.connect(600_000, 100_000, True)
    read = await service.read(2048, 0)
    await service.disconnect()
    check("board accepts RX after a client is killed", read["sample_count"] == 2048)


async def two_clients(config: Settings) -> None:
    """The app has one owner per role. A second client is refused and the first is untouched."""
    first, second = Service(config), Service(config)
    await first.connect(600_000, 100_000, True)
    await first.read(2048, 0)
    try:
        await second.connect(600_000, 100_000, True)
        refused_at_connect = False
    except ConnectionError:
        refused_at_connect = True
    check("two clients: second is refused at connect", refused_at_connect)
    check("two clients: second reports no session", second.status()["session_open"] is False)
    check("two clients: first still reads", (await first.read(2048, 0))["sample_count"] == 2048)
    await first.disconnect()
    await second.disconnect()
    fresh = Service(config)
    await fresh.connect(600_000, 100_000, True)
    read = await fresh.read(2048, 0)
    await fresh.disconnect()
    check("two clients: a fresh client works after", read["sample_count"] == 2048)


async def app_dies(host: str, config: Settings) -> None:
    service = Service(config)
    await service.connect(600_000, 100_000, True)
    await service.read(2048, 0)
    await asyncio.to_thread(app, host, "stop")
    start = time.monotonic()
    error = ""
    while time.monotonic() - start < 8:
        try:
            await service.read(8192, 0)
        except (ConnectionError, OSError, TimeoutError) as caught:
            error = type(caught).__name__
            break
    check(
        "app stopped mid-session: read fails cleanly",
        bool(error),
        f"{error}, {time.monotonic() - start:.1f} s",
    )
    status = service.status()
    check(
        "app stopped mid-session: status closed, settings cleared",
        status["session_open"] is False and status["requested_settings"] is None,
    )
    down = await service.diagnostics()
    check(
        "app down: metadata file still served, so it proves nothing about running",
        down["evidence"]["/sdr_transceiver/info/info.json"]["available"] is True,
    )
    try:
        await service.connect(600_000, 100_000, True)
        failed = False
    except ConnectionError:
        failed = True
    check("app down: connect reports ConnectionError", failed)
    await asyncio.to_thread(app, host, "start")
    await asyncio.sleep(APP_START_S)
    await service.connect(600_000, 100_000, True)
    read = await service.read(2048, 0)
    await service.disconnect()
    check("same server recovers after the app restarts", read["sample_count"] == 2048)


async def wrong_targets(host: str, ssh_user: str) -> None:
    last = int(host.rsplit(".", 1)[1])
    absent = host.rsplit(".", 1)[0] + f".{last + 1 if last < 250 else last - 1}"
    config = Settings(mode="live", host=absent, timeout=0.5)
    start = time.monotonic()
    probe = await probe_tcp(config)
    check(
        "absent host: probe bounded, with a reason",
        probe["reachable"] is False and time.monotonic() - start < 2,
        probe["failure"],
    )
    diagnostics = await Service(config).diagnostics()
    check(
        "absent host: diagnostics report unavailable",
        not any(item["available"] for item in diagnostics["evidence"].values()),
    )
    if ssh_user:
        closed = Settings(mode="live", host=host, ssh_user=ssh_user, ssh_port=2, timeout=2)
        system = await read_system(closed)
        check("closed SSH port: reported unavailable", system["available"] is False)


async def real_images(host: str, ssh_user: str) -> None:
    with tempfile.TemporaryDirectory() as root:
        for name in ("fpga.bit.bin", "fpga.dtbo"):
            done = await asyncio.to_thread(
                subprocess.run,
                [
                    "scp",
                    "-q",
                    "-O",
                    "-F",
                    "/dev/null",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    f"{ssh_user}@{host}:{APP_DIR}/{name}",
                    f"{root}/{name}",
                ],
                capture_output=True,
                timeout=60,
            )
            check(f"real image copied: {name}", done.returncode == 0, done.stderr[-120:])
        overlay = (Path(root) / "fpga.dtbo").read_bytes()
        check("installed overlay names the installed bitstream", b"fpga.bit.bin" in overlay)
        async with session(
            RP_MODE="live", RP_HOST=host, RP_SSH_USER=ssh_user, RP_PROJECT_ROOT=root
        ) as client:
            evidence = (await data(client, "rp_system_diagnostics"))["evidence"]
            hashes = {
                "fpga.bit.bin": evidence["installed_bitstream_sha256"]["text"].split()[0],
                "fpga.dtbo": evidence["installed_dtbo_sha256"]["text"].split()[0],
            }
            for name, expected in hashes.items():
                result = await data(
                    client, "rp_fpga_inspect_artifact", path=name, expected_sha256=expected
                )
                check(
                    f"local hash matches board hash: {name}",
                    result["hash_matches"] is True,
                    f"{result['size']} bytes",
                )
            manifest = await data(
                client,
                "rp_fpga_manifest",
                bitstream_path="fpga.bit.bin",
                dtbo_path="fpga.dtbo",
                part="xc7z010clg400-1",
            )
            check("manifest of the real image stays unapproved", manifest["approved"] is False)


async def board(host: str, ssh_user: str, seconds: float) -> None:
    config = Settings(mode="live", host=host, allow_rx=True, timeout=10)
    if (await probe_tcp(config))["reachable"]:
        raise SystemExit("The SDR app is already running. Stop it first: it may be in use.")
    await wrong_targets(host, ssh_user)
    if ssh_user:
        await real_images(host, ssh_user)
    app(host, "start")
    try:
        await asyncio.sleep(APP_START_S)
        await limits(host)
        await tuning(config)
        await continuity(config, seconds)
        await diagnostics_during_stream(config, ssh_user, seconds)
        await churn(config, 60)
        await client_dies(host, config)
        await two_clients(config)
        await app_dies(host, config)
    finally:
        app(host, "stop")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="")
    parser.add_argument("--i-own-this-idle-board", action="store_true")
    parser.add_argument("--ssh-user", default="")
    parser.add_argument("--stream-seconds", type=float, default=20)
    parser.add_argument("--pinned-source", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.host and not args.i_own_this_idle_board:
        parser.error("--host requires --i-own-this-idle-board")

    try:
        await plans_and_profile(args.pinned_source)
        await project_tools()
        if args.host:
            await board(args.host, args.ssh_user, args.stream_seconds)
    finally:
        failed = [item for item in RESULTS if not item["pass"]]
        print(f"{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed")
        if args.report:
            args.report.write_text(json.dumps(RESULTS, indent=1, default=str) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
