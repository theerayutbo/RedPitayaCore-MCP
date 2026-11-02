#!/usr/bin/env python3
"""Operator-run RX-only acceptance test on an idle STEMlab 125-14. Needs numpy.

Usage: python scripts/live_acceptance.py --host BOARD_IPV4 --i-own-this-idle-board

The script starts and stops sdr_transceiver with the board's own API. That is the
operator's procedure, and the MCP server itself still cannot do it. It opens only RX roles
and never keys TX. The test tone is the ADC's own DC offset: tuning the receiver to f
places it at -f in baseband, so no signal source is needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.protocol import RATES
from redpitayacore_mcp.service import Service

BLOCKS = 32
MAX_SPUR_DBC = -60.0
MIN_RAW_SPUR_DBC = -30.0
CONTROL_SETTLE_S = 0.2
RESULTS: list[dict[str, object]] = []


def check(name: str, passed: bool, detail: object = "") -> None:
    RESULTS.append({"check": name, "pass": bool(passed), "detail": detail})
    print(f"{'PASS' if passed else 'FAIL'}  {name}  {detail}", flush=True)


def app(host: str, action: str) -> None:
    url = (
        f"http://{host}/bazaar?start=sdr_transceiver?type=run"
        if action == "start"
        else f"http://{host}/bazaar?stop=sdr_transceiver"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=10) as response:  # noqa: S310 - fixed operator URL
        body = json.loads(response.read(200))
    if body.get("status") != "OK":
        raise RuntimeError(f"Board app {action} failed: {body}")


async def mcp_session(host: str, allow_rx: bool):
    env = {
        **os.environ,
        "RP_MODE": "live",
        "RP_HOST": host,
        "RP_ALLOW_RX": "true" if allow_rx else "false",
        "RP_ALLOW_LIFECYCLE": "false",
        "RP_ALLOW_PROJECT_WRITE": "false",
        "RP_ALLOW_LOCAL_BUILD": "false",
        "RP_SSH_USER": "",
    }
    return StdioServerParameters(command=sys.executable, args=["-m", "redpitayacore_mcp"], env=env)


async def call(session: ClientSession, tool: str, **arguments):
    return await session.call_tool(tool, arguments)


async def tools_with_app_down(host: str) -> None:
    async with stdio_client(await mcp_session(host, False)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            probe = (await call(session, "rp_connection_probe")).structuredContent
            check(
                "app down: probe reports a cause",
                probe["reachable"] is False and probe.get("failure") in {"refused", "timeout"},
                probe.get("failure"),
            )


async def tools_rx_disabled(host: str) -> None:
    async with stdio_client(await mcp_session(host, False)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            refused = await call(
                session,
                "rp_rx_connect",
                frequency_hz=7_100_000,
                sample_rate=50_000,
                acknowledge_side_effects=True,
            )
            check("RX stays off without RP_ALLOW_RX", refused.isError)


async def tools_with_app_up(host: str) -> None:
    async with stdio_client(await mcp_session(host, True)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            data = (await call(session, "rp_device_diagnostics")).structuredContent
            info = data["evidence"]["/sdr_transceiver/info/info.json"]
            check("HTTP metadata", info["available"], info.get("data", {}).get("version"))
            check("no /bazaar path requested", not any("bazaar" in key for key in data["evidence"]))
            probe = (await call(session, "rp_connection_probe")).structuredContent
            check("app still up after diagnostics: probe reachable", probe["reachable"] is True)

            plan = (await call(session, "rp_app_lifecycle", action="stop")).structuredContent
            check("lifecycle dry run does not execute", plan["executed"] is False)
            check(
                "live lifecycle refused",
                (await call(session, "rp_app_lifecycle", action="stop", dry_run=False)).isError,
            )
            check(
                "RX refused without acknowledgement",
                (await call(session, "rp_rx_connect", frequency_hz=7_100_000)).isError,
            )
            system = (await call(session, "rp_system_diagnostics")).structuredContent
            check("SSH off is reported, not faked", system["available"] is False)
            fpga = (await call(session, "rp_fpga_diagnostics")).structuredContent
            check("FPGA diagnostics read no registers", fpga["registers_read"] is False)

            for cycle in (1, 2):
                opened = await call(
                    session,
                    "rp_rx_connect",
                    frequency_hz=7_100_000,
                    sample_rate=50_000,
                    acknowledge_side_effects=True,
                )
                check(f"rx_connect cycle {cycle}", not opened.isError)
                check(
                    f"second connect refused, cycle {cycle}",
                    (
                        await call(
                            session,
                            "rp_rx_connect",
                            frequency_hz=7_100_000,
                            sample_rate=50_000,
                            acknowledge_side_effects=True,
                        )
                    ).isError,
                )
                for count in (1, 7, 2048, 8192):
                    result = await call(session, "rp_rx_read", sample_count=count)
                    content = result.structuredContent or {}
                    check(
                        f"rx_read {count}, cycle {cycle}",
                        not result.isError
                        and content["sample_count"] == count
                        and content["sample_order"] == "glibc_memcpy",
                        content.get("rms_magnitude"),
                    )
                await call(session, "rp_device_diagnostics")
                await call(session, "rp_connection_probe")
                during = await call(session, "rp_rx_read", sample_count=2048)
                check(f"stream survives diagnostics, cycle {cycle}", not during.isError)
                status = (await call(session, "rp_connection_status")).structuredContent
                check(f"status reports open, cycle {cycle}", status["session_open"] is True)
                closed = (await call(session, "rp_rx_disconnect")).structuredContent
                check(f"disconnect, cycle {cycle}", closed["session_open"] is False)


async def capture(config: Settings, frequency: int, rate: int) -> np.ndarray:
    service = Service(config)
    await asyncio.sleep(CONTROL_SETTLE_S)
    await service.connect(frequency, rate, True)
    try:
        await asyncio.sleep(CONTROL_SETTLE_S)
        values: list[tuple[float, float]] = []
        for _ in range(BLOCKS // 4):
            values += await service.rx.read(8192)
    finally:
        await service.disconnect()
    pairs = np.array(values, dtype=np.float64)
    return pairs[:, 0] + 1j * pairs[:, 1]


def analyse(samples: np.ndarray, frequency: int, rate: int) -> tuple[float, float]:
    samples = samples[2048:]
    window = np.hanning(samples.size)
    spectrum = 20 * np.log10(np.abs(np.fft.fft(samples * window)) / window.sum() + 1e-15)
    bins = np.fft.fftfreq(samples.size, 1 / rate)
    peak = int(np.argmax(spectrum))
    spurs = []
    for k in range(1, 8):
        target = (bins[peak] + k * rate / 8 + rate / 2) % rate - rate / 2
        index = int(np.argmin(np.abs(bins - target)))
        spurs.append(spectrum[max(index - 3, 0) : index + 4].max() - spectrum[peak])
    return float(bins[peak]), float(max(spurs))


async def signal_chain(host: str) -> None:
    corrected = Settings(mode="live", host=host, allow_rx=True, timeout=10)
    raw = replace(corrected, rx_sample_order="raw")
    for rate in RATES:
        frequency = rate // 10
        samples = await capture(corrected, frequency, rate)
        tone, spur = analyse(samples, frequency, rate)
        tolerance = 2 * rate / samples.size
        check(f"tone at -f, {rate} S/s", abs(tone + frequency) <= tolerance, f"{tone:.2f} Hz")
        check(f"spurs below {MAX_SPUR_DBC} dBc, {rate} S/s", spur < MAX_SPUR_DBC, f"{spur:.1f} dBc")
    samples = await capture(raw, 5_000, 50_000)
    _, spur = analyse(samples, 5_000, 50_000)
    check("raw order shows the defect the fix removes", spur > MIN_RAW_SPUR_DBC, f"{spur:.1f} dBc")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--i-own-this-idle-board", action="store_true", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    await tools_with_app_down(args.host)
    app(args.host, "start")
    try:
        await asyncio.sleep(4)
        await tools_rx_disabled(args.host)
        await tools_with_app_up(args.host)
        await signal_chain(args.host)
    finally:
        app(args.host, "stop")
    await asyncio.sleep(2)
    await tools_with_app_down(args.host)

    failed = [item for item in RESULTS if not item["pass"]]
    print(f"{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed")
    if args.report:
        args.report.write_text(json.dumps(RESULTS, indent=1) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
