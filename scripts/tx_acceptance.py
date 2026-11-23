#!/usr/bin/env python3
"""Operator-run TX acceptance test over a cabled OUT to IN loop. Needs numpy.

Usage: PYTHONPATH=src:scripts python scripts/tx_acceptance.py --host BOARD_IPV4 \\
       --i-own-this-idle-board --i-cabled-out-to-in --channel 1 [--ssh-user USER]

This drives RF. Run it only with OUT cabled to IN of the chosen channel, straight or through
an attenuator, with no antenna or amplifier on any connector. The receiver is the measuring
instrument, so levels are relative: nothing here calibrates volts or dBm. --ssh-user adds
PTT checks, which read the pin's register with the board's own monitor tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from live_acceptance import RESULTS, app, check
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import redpitayacore_mcp.protocol as protocol
from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.protocol import RATES, tone_block
from redpitayacore_mcp.rx_order import BLOCK_BYTES, restore_block
from redpitayacore_mcp.service import Service
from redpitayacore_mcp.transport import TX_FIFO_SAMPLES, TX_MIN_LEAD_BLOCKS

RX_RATE = 100_000
RX_CODE = RATES.index(RX_RATE)
BASE_HZ = 1_000_000
OFFSET_HZ = 5_000.0
AMPLITUDE = 0.1
FREQUENCIES = (
    100_000,
    1_000_000,
    3_600_000,
    7_100_000,
    14_200_000,
    21_200_000,
    28_500_000,
    50_100_000,
)
AMPLITUDES = (0.02, 0.05, 0.1, 0.2, 0.5)
# Below 0.02 the level error repeats exactly but moves with the carrier, so it is measured here
# and documented, not judged.
SMALL_AMPLITUDES = (0.002, 0.005, 0.01)
MIN_SPUR_DBC = 50.0
# Edge splatter 3 kHz or more from the tone. A hard edge measured -32 dBc at 50 kS/s.
MAX_EDGE_DBC = -50.0
LEAD_S = 0.1
MIN_TONE_DB = 40.0
MAX_LINEARITY_DB = 0.5
MAX_KILLED_TAIL_S = 0.5
# The server's default after TX control closes: tone offsets scale by 100 kS/s over the rate.
DEFAULT_HZ = 600_000
DEFAULT_RATE = 100_000
MAX_RESIDUAL_DB = -70.0
PHASE_STEP_RAD = 0.1
SAMPLES_PER_BLOCK = 2048
WORD = struct.Struct("<I")
PORTS = {1: 1001, 2: 1002}
GPIO_ADDRESS = {1: "0x40000000", 2: "0x80000000"}


class Rx(threading.Thread):
    """Continuous receiver that keeps a sample index per timeline mark."""

    def __init__(self, host: str, port: int, frequency: int):
        super().__init__(daemon=True)
        self.control = self._role(host, port, 0)
        self.control.sendall(WORD.pack(frequency) + WORD.pack(1 << 28 | RX_CODE))
        time.sleep(0.05)
        self.data = self._role(host, port, 1)
        self.data.settimeout(3)
        self.pending = bytearray()
        self.blocks: list[bytes] = []
        self.count = 0
        self.running = True
        self.start()

    @staticmethod
    def _role(host: str, port: int, role: int) -> socket.socket:
        client = socket.create_connection((host, port), timeout=3)
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.sendall(WORD.pack(role))
        return client

    def run(self) -> None:
        while self.running:
            self.pending += self.data.recv(65536)
            while len(self.pending) >= BLOCK_BYTES:
                self.blocks.append(restore_block(bytes(self.pending[:BLOCK_BYTES])))
                del self.pending[:BLOCK_BYTES]
                self.count += SAMPLES_PER_BLOCK

    def finish(self) -> np.ndarray:
        self.running = False
        self.join(4)
        self.data.close()
        self.control.close()
        time.sleep(0.25)
        return np.frombuffer(b"".join(self.blocks), "<c8")


def spectrum(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    window = np.blackman(len(samples))
    level = np.abs(np.fft.fftshift(np.fft.fft(samples * window))) / window.sum()
    return (
        np.fft.fftshift(np.fft.fftfreq(len(samples), 1 / RX_RATE)),
        20 * np.log10(level + 1e-13),
    )


def measure(samples: np.ndarray, tone: float) -> dict[str, float]:
    frequency, level = spectrum(samples)
    at = np.abs(frequency - tone) < 100
    peak = float(level[at].max())
    rest = np.where(np.abs(frequency - tone) > 200, level, -400)
    worst = int(np.argmax(rest))
    return {
        "tone_db": round(peak, 2),
        "above_floor_db": round(peak - float(np.median(level)), 1),
        "spur_dbc": round(peak - float(level[worst]), 1),
        "spur_hz": round(float(frequency[worst])),
    }


def level(samples: np.ndarray, tone: float) -> float:
    """Coherent tone level in dB. FFT peaks scallop by up to 0.7 dB as the window length moves."""
    count = len(samples)
    n = np.arange(count) / count
    window = (
        0.35875
        - 0.48829 * np.cos(2 * np.pi * n)
        + 0.14128 * np.cos(4 * np.pi * n)
        - 0.01168 * np.cos(6 * np.pi * n)
    )
    mixed = samples * np.exp(-2j * np.pi * tone * np.arange(count) / RX_RATE)
    return 20 * float(np.log10(abs(np.sum(window * mixed)) / window.sum()))


def gaps(samples: np.ndarray, tone: float) -> tuple[int, int, float]:
    """Check the whole burst between its edges: phase steps, and envelope dips below 90 %."""
    first, last = on_time(samples, tone)
    body = samples[int((first + 0.02) * RX_RATE) : int((last - 0.02) * RX_RATE)]
    steps, worst = phase_steps(body, tone)
    level_ = envelope(body, tone)
    dips = int(np.count_nonzero(level_ < 0.9 * np.median(level_)))
    return steps, dips, worst


def phase_steps(samples: np.ndarray, tone: float) -> tuple[int, float]:
    mixed = samples * np.exp(-2j * np.pi * tone * np.arange(len(samples)) / RX_RATE)
    smooth = np.convolve(mixed, np.ones(64) / 64, "valid")
    step = np.abs(np.angle(smooth[64:] * np.conj(smooth[:-64])))
    # A run that is already above the threshold at the first sample counts too.
    above = np.concatenate([[False], step > PHASE_STEP_RAD]).astype(int)
    return int(np.count_nonzero(np.diff(above) == 1)), float(step.max())


def envelope(samples: np.ndarray, tone: float, size: int = 256) -> np.ndarray:
    mixed = samples * np.exp(-2j * np.pi * tone * np.arange(len(samples)) / RX_RATE)
    usable = len(mixed) // size
    return np.abs(mixed[: usable * size].reshape(usable, size).mean(axis=1))


def on_time(samples: np.ndarray, tone: float, size: int = 256) -> tuple[float, float]:
    """Return when the tone first and last exceeds half its peak, in seconds."""
    level = envelope(samples, tone, size)
    active = np.flatnonzero(level > level.max() / 2)
    return float(active[0] * size / RX_RATE), float((active[-1] + 1) * size / RX_RATE)


def queue_s(rate: int) -> float:
    block_s = SAMPLES_PER_BLOCK / rate
    fifo_s = TX_FIFO_SAMPLES / rate
    return max(TX_MIN_LEAD_BLOCKS * block_s, LEAD_S + fifo_s + block_s) + fifo_s


def tx_config(host: str, port: int, top: float = AMPLITUDE) -> Settings:
    return Settings(
        mode="live",
        host=host,
        port=port,
        allow_tx=True,
        tx_min_hz=1,
        tx_max_hz=60_000_000,
        tx_max_amplitude=top,
        tx_max_seconds=20,
    )


async def burst(service: Service, frequency: int, rate: int, seconds: float, **changes) -> float:
    arguments = {
        "frequency_hz": frequency,
        "sample_rate": rate,
        "tone_offset_hz": OFFSET_HZ,
        "amplitude": AMPLITUDE,
        "duration_s": seconds,
        "key_ptt": False,
        "acknowledge_rf_output": True,
        **changes,
    }
    result = await service.tx_start(**arguments)
    return result["requested_settings"]["tone_offset_hz"]


async def wait_done(service: Service) -> None:
    if service.tx.task is not None:
        await asyncio.wait_for(asyncio.shield(service.tx.task), 30)


async def every_rate(host: str, port: int) -> None:
    for rate in RATES:
        rx = Rx(host, port, BASE_HZ)
        await asyncio.sleep(0.3)
        start = rx.count
        service = Service(tx_config(host, port))
        tone = await burst(service, BASE_HZ, rate, 4.0)
        await wait_done(service)
        status = service.tx_status()
        await asyncio.sleep(0.5)
        samples = rx.finish()[start:]
        first, last = on_time(samples, tone)
        begin = int((first + 0.02) * RX_RATE)
        onset = samples[begin : begin + RX_RATE // 2]
        steps, worst = phase_steps(onset, tone)
        check(f"rate {rate}: clean onset", steps == 0, f"{steps} steps, worst {worst:.3f} rad")
        before = samples[: max(int((first - 0.02) * RX_RATE), 256)]
        quiet = float(np.abs(before - before.mean()).max() / envelope(samples, tone).max())
        check(f"rate {rate}: nothing before the tone", quiet < 0.1, f"{quiet:.4f} of the tone")
        body = samples[RX_RATE : 3 * RX_RATE]
        result = measure(body, tone)
        steps, dips, worst = gaps(samples, tone)
        check(f"rate {rate}: burst completed", status["outcome"] == "completed", status["outcome"])
        check(f"rate {rate}: tone present", result["above_floor_db"] > MIN_TONE_DB, result)
        check(f"rate {rate}: spur", result["spur_dbc"] > MIN_SPUR_DBC, result["spur_dbc"])
        check(
            f"rate {rate}: no gaps in the whole burst",
            steps == 0 and dips == 0,
            f"{steps} steps, {dips} dips, worst {worst:.3f} rad",
        )


async def every_frequency(host: str, port: int) -> None:
    for frequency in FREQUENCIES:
        rx = Rx(host, port, frequency)
        await asyncio.sleep(0.3)
        start = rx.count
        service = Service(tx_config(host, port))
        tone = await burst(service, frequency, 50_000, 1.5)
        await wait_done(service)
        samples = rx.finish()
        result = measure(samples[start + RX_RATE // 2 : start + RX_RATE], tone)
        check(f"frequency {frequency}: tone", result["above_floor_db"] > MIN_TONE_DB, result)


async def linearity(host: str, port: int) -> None:
    levels: dict[float, float] = {}
    for amplitude in (*SMALL_AMPLITUDES, *AMPLITUDES):
        rx = Rx(host, port, BASE_HZ)
        await asyncio.sleep(0.3)
        start = rx.count
        service = Service(tx_config(host, port, top=max(AMPLITUDES)))
        tone = await burst(service, BASE_HZ, 50_000, 1.5, amplitude=amplitude)
        await wait_done(service)
        samples = rx.finish()
        levels[amplitude] = level(samples[start + RX_RATE // 2 : start + RX_RATE], tone)
    reference = levels[AMPLITUDE]
    for amplitude, value in levels.items():
        error = value - reference - 20 * float(np.log10(amplitude / AMPLITUDE))
        if amplitude in SMALL_AMPLITUDES:
            print(f"MEASURE  amplitude {amplitude}: {error:+.2f} dB", flush=True)
        else:
            check(
                f"amplitude {amplitude}: linear", abs(error) < MAX_LINEARITY_DB, f"{error:+.2f} dB"
            )


async def timing(host: str, port: int) -> None:
    for rate in RATES:
        rx = Rx(host, port, BASE_HZ)
        await asyncio.sleep(0.3)
        start = rx.count
        service = Service(tx_config(host, port))
        tone = await burst(service, BASE_HZ, rate, 2.0)
        await wait_done(service)
        await asyncio.sleep(0.7)
        samples = rx.finish()[start:]
        first, last = on_time(samples, tone)
        asked = service.tx_status()["requested_settings"]["duration_s"]
        block = SAMPLES_PER_BLOCK / rate
        length = last - first
        limit = 2 * block + 0.005
        check(
            f"rate {rate}: burst length",
            abs(length - asked) <= limit,
            f"{length:.3f} of {asked:.3f} s",
        )
        tail = samples[int((last + 0.2) * RX_RATE) :]
        peak = float(envelope(samples, tone).max())
        mixed = tail * np.exp(-2j * np.pi * tone * np.arange(len(tail)) / RX_RATE)
        # Coherent over the whole tail: short windows sit on the noise floor at -54 dB.
        residual = 20 * float(np.log10(abs(mixed.mean()) / peak))
        check(f"rate {rate}: silent after", residual < MAX_RESIDUAL_DB, f"{residual:.1f} dB")

        rx = Rx(host, port, BASE_HZ)
        await asyncio.sleep(0.3)
        start = rx.count
        service = Service(tx_config(host, port))
        tone = await burst(service, BASE_HZ, rate, 8.0)
        await asyncio.sleep(1.0)
        asked_at = rx.count - start
        await service.tx_stop()
        await asyncio.sleep(0.7)
        samples = rx.finish()[start:]
        _, last = on_time(samples, tone)
        after = last - asked_at / RX_RATE
        bound = queue_s(rate) + 0.1
        check(f"rate {rate}: stop ends RF", after < bound, f"{after:.3f} s, bound {bound:.3f}")
        outcome = service.tx_status()["outcome"]
        check(f"rate {rate}: stop outcome", outcome == "stopped", outcome)


def edge_splatter(samples: np.ndarray, tone: float, at: float) -> float:
    """Strongest component at least 3 kHz from the tone, in a 512-sample window at one moment."""
    middle = int(at * RX_RATE)
    piece = samples[middle - 256 : middle + 256]
    window = np.hanning(512)
    spectrum_ = np.abs(np.fft.fftshift(np.fft.fft(piece * window))) / window.sum()
    frequency = np.fft.fftshift(np.fft.fftfreq(512, 1 / RX_RATE))
    power = 20 * np.log10(spectrum_ + 1e-13)
    return float(power[np.abs(frequency - tone) > 3000].max())


async def splatter(host: str, port: int) -> None:
    """Burst edges must not spread energy beside the tone. A hard edge is the control."""
    for rate in (20_000, 50_000, 250_000, 1_250_000):
        results = {}
        for name, ramp_s in (("ramped", protocol.TX_RAMP_S), ("hard edge", 1e-9)):
            saved, protocol.TX_RAMP_S = protocol.TX_RAMP_S, ramp_s
            try:
                rx = Rx(host, port, BASE_HZ)
                await asyncio.sleep(0.3)
                start = rx.count
                service = Service(tx_config(host, port, top=0.5))
                tone = await burst(service, BASE_HZ, rate, 0.6, amplitude=0.5)
                await wait_done(service)
                await asyncio.sleep(0.2)
                samples = rx.finish()[start:]
            finally:
                protocol.TX_RAMP_S = saved
            first, last = on_time(samples, tone, 64)
            steady = edge_splatter(samples, tone, (first + last) / 2)
            peak = 20 * float(np.log10(envelope(samples, tone, 64).max()))
            worst = max(edge_splatter(samples, tone, first), edge_splatter(samples, tone, last))
            results[name] = worst - peak
            print(
                f"MEASURE  rate {rate} {name}: edges {worst - peak:.1f} dBc, "
                f"steady {steady - peak:.1f} dBc",
                flush=True,
            )
        ramped, hard = results["ramped"], results["hard edge"]
        check(f"rate {rate}: ramped edges", ramped < MAX_EDGE_DBC, f"{ramped:.1f} dBc")
        check(f"rate {rate}: hard edge is caught", hard > MAX_EDGE_DBC, f"{hard:.1f} dBc")


async def no_rf_at_default(host: str, port: int) -> None:
    """A clean end must not let queued tone reach the server's 600 kHz default."""
    for rate in (20_000, 50_000, 250_000):
        for how in ("completes", "stopped"):
            rx = Rx(host, port, DEFAULT_HZ)
            await asyncio.sleep(0.3)
            start = rx.count
            service = Service(tx_config(host, port))
            tone = await burst(service, 7_100_000, rate, 0.5 if how == "completes" else 5.0)
            if how == "stopped":
                await asyncio.sleep(0.3)
                await service.tx_stop()
            await wait_done(service)
            await asyncio.sleep(0.8)
            samples = rx.finish()[start:]
            moved = tone * DEFAULT_RATE / rate
            mixed = samples * np.exp(-2j * np.pi * moved * np.arange(len(samples)) / RX_RATE)
            size = 1024
            usable = len(mixed) // size
            level = np.abs(mixed[: usable * size].reshape(usable, size).mean(axis=1))
            ratio = float(level.max() / np.median(level))
            check(
                f"rate {rate}, {how}: nothing at 600 kHz", ratio < 10, f"peak {ratio:.1f} x median"
            )


async def stall(host: str, port: int) -> None:
    """Block the loop mid-burst: within RP_TX_LEAD_S no gap may show, far beyond it one must."""
    for rate in (20_000, 50_000, 250_000):
        for seconds, expect_gap in ((0.8 * LEAD_S, False), (1.5, True)):
            rx = Rx(host, port, BASE_HZ)
            await asyncio.sleep(0.3)
            start = rx.count
            service = Service(tx_config(host, port))
            tone = await burst(service, BASE_HZ, rate, 3.0)
            await asyncio.sleep(1.0)
            time.sleep(seconds)  # noqa: ASYNC251 - the host stall under test
            await wait_done(service)
            await asyncio.sleep(0.3)
            samples = rx.finish()[start:]
            steps, dips, worst = gaps(samples, tone)
            found = steps > 0 or dips > 0
            name = "control: long stall is caught" if expect_gap else "stall within lead"
            detail = f"{seconds:.2f} s: {steps} steps, {dips} dips"
            check(f"rate {rate}: {name}", found == expect_gap, detail)


async def second_client(host: str, port: int) -> None:
    rx = Rx(host, port, BASE_HZ)
    await asyncio.sleep(0.3)
    start = rx.count
    first = Service(tx_config(host, port))
    tone = await burst(first, BASE_HZ, 50_000, 3.0)
    await asyncio.sleep(0.5)
    second = Service(tx_config(host, port))
    refused = ""
    before = rx.count - start
    try:
        await burst(second, BASE_HZ, 50_000, 0.5, tone_offset_hz=9_000.0)
    except ConnectionError as error:
        refused = str(error)
    after = rx.count - start
    check("second TX client refused", bool(refused), refused)
    await wait_done(first)
    samples = rx.finish()[start:]
    check("first burst finished", first.tx_status()["outcome"] == "completed", first.tx_status())
    # Cover the second client's sockets, plus the delay before RX delivers what the DAC played.
    window = samples[before - RX_RATE // 5 : after + RX_RATE // 2]
    steps, worst = phase_steps(window, tone)
    check("first burst undisturbed", steps == 0, f"{steps} steps, worst {worst:.3f} rad")


async def killed_process(host: str, port: int) -> None:
    code = (
        "import asyncio,sys\n"
        "from redpitayacore_mcp.config import Settings\n"
        "from redpitayacore_mcp.service import Service\n"
        "async def go():\n"
        f"    s=Service(Settings(mode='live',host={host!r},port={port},allow_tx=True,"
        "tx_min_hz=1,tx_max_hz=60000000,tx_max_seconds=20))\n"
        f"    await s.tx_start({BASE_HZ},50000,{OFFSET_HZ},{AMPLITUDE},15.0,False,True)\n"
        "    print('up',flush=True)\n"
        "    await asyncio.sleep(30)\n"
        "asyncio.run(go())\n"
    )
    tone = tone_block(50_000, OFFSET_HZ, AMPLITUDE)[1]
    for listen, name in ((BASE_HZ, "carrier"), (DEFAULT_HZ, "600 kHz")):
        rx = Rx(host, port, listen)
        await asyncio.sleep(0.3)
        start = rx.count
        process = subprocess.Popen(  # noqa: S603, ASYNC220 - fixed code, operator test
            [sys.executable, "-c", code], stdout=subprocess.PIPE, env={**os.environ}
        )
        assert process.stdout is not None
        check(f"killed at {name}: burst up", process.stdout.readline().strip() == b"up", "")
        await asyncio.sleep(1.5)
        killed = rx.count - start
        process.send_signal(signal.SIGKILL)
        process.wait(5)
        await asyncio.sleep(1.2)
        samples = rx.finish()[start:]
        await asyncio.sleep(0.5)
        if listen == BASE_HZ:
            result = measure(samples[killed - RX_RATE // 2 : killed], tone)
            check(
                "killed: tone present before the kill",
                result["above_floor_db"] > MIN_TONE_DB,
                result,
            )
            _, last = on_time(samples, tone)
            gone = last - killed / RX_RATE
            check("killed: carrier ends", gone < MAX_KILLED_TAIL_S, f"{gone:.3f} s after SIGKILL")
        else:
            moved = tone * DEFAULT_RATE / 50_000
            _, last = on_time(samples, moved)
            gone = last - killed / RX_RATE
            print(f"MEASURE  tail at 600 kHz after SIGKILL: {gone:.3f} s", flush=True)
            check("killed: 600 kHz tail is bounded", gone < MAX_KILLED_TAIL_S, f"{gone:.3f} s")
    after = Service(tx_config(host, port))
    await burst(after, BASE_HZ, 50_000, 0.3)
    await wait_done(after)
    check("killed: TX reusable", after.tx_status()["outcome"] == "completed", "")


async def full_duplex(host: str, port: int) -> None:
    rx = Rx(host, port, BASE_HZ)
    await asyncio.sleep(0.3)
    start = rx.count
    service = Service(tx_config(host, port))
    tone = await burst(service, BASE_HZ, 100_000, 10.0)
    await asyncio.sleep(1.0)
    begin, first_count = time.monotonic(), rx.count
    await asyncio.sleep(8.0)
    rate = (rx.count - first_count) / (time.monotonic() - begin)
    await wait_done(service)
    samples = rx.finish()
    body = samples[start + RX_RATE : start + 9 * RX_RATE]
    steps, worst = phase_steps(body, tone)
    result = measure(body, tone)
    check("full duplex: RX kept up", rate > 0.95 * RX_RATE, f"{rate:.0f} S/s")
    check("full duplex: no gaps in 8 s", steps == 0, f"{steps} steps, worst {worst:.3f} rad")
    check("full duplex: spur", result["spur_dbc"] > MIN_SPUR_DBC, result)


def text(result) -> str:
    return result.content[0].text if result.isError else ""


async def through_mcp(host: str, port: int) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "redpitayacore_mcp"],
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("RP_")},
            "PYTHONPATH": "src",
            "RP_MODE": "live",
            "RP_HOST": host,
            "RP_PORT": str(port),
            "RP_ALLOW_RX": "true",
            "RP_ALLOW_TX": "true",
            "RP_TX_MIN_HZ": "900000",
            "RP_TX_MAX_HZ": "1100000",
        },
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        arguments = {"frequency_hz": 1_000_000, "duration_s": 0.5, "amplitude": 0.05}

        async def call(tool: str, **values):
            return await session.call_tool(tool, values)

        no_ack = await call("rp_tx_start", **arguments)
        check("MCP: refused without acknowledgement", "acknowledge_rf_output" in text(no_ack), "")
        out_of_band = await call(
            "rp_tx_start", **{**arguments, "frequency_hz": 7_100_000}, acknowledge_rf_output=True
        )
        check("MCP: refused out of band", "operator band" in text(out_of_band), "")
        loud = await call(
            "rp_tx_start", **{**arguments, "amplitude": 0.5}, acknowledge_rf_output=True
        )
        check("MCP: refused above the limit", "at most 0.1" in text(loud), "")
        long = await call(
            "rp_tx_start", **{**arguments, "duration_s": 11.0}, acknowledge_rf_output=True
        )
        check("MCP: refused above the time limit", "at most 10" in text(long), "")

        opened = await call(
            "rp_rx_connect",
            frequency_hz=1_000_000,
            sample_rate=50_000,
            acknowledge_side_effects=True,
        )
        check("MCP: RX open", not opened.isError, "")
        quiet = await call("rp_rx_read", sample_count=2048, preview_samples=0)
        started = await call(
            "rp_tx_start", **{**arguments, "duration_s": 1.0}, acknowledge_rf_output=True
        )
        check("MCP: TX started", not started.isError, started.structuredContent)
        await asyncio.sleep(0.4)
        status = await call("rp_tx_status")
        check("MCP: status active", status.structuredContent["tx_active"], "")
        loud_read = None
        for _ in range(12):
            reading = await call("rp_rx_read", sample_count=2048, preview_samples=0)
            loud_read = reading.structuredContent["mean_power"]
        await call("rp_tx_stop")
        ended = await call("rp_tx_status")
        check("MCP: burst ended", not ended.structuredContent["tx_active"], ended.structuredContent)
        gain = 10 * float(np.log10(loud_read / max(quiet.structuredContent["mean_power"], 1e-30)))
        check("MCP: RX saw the burst", gain > 10, f"{gain:.1f} dB above quiet")
        await call("rp_rx_disconnect")


def read_gpio(host: str, user: str, channel: int) -> int:
    done = subprocess.run(  # noqa: S603, S607 - fixed read of one register
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            f"{user}@{host}",
            f"/opt/redpitaya/bin/monitor {GPIO_ADDRESS[channel]}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return (int(done.stdout.strip(), 16) >> 16) & 0xFF


async def ptt(host: str, port: int, user: str, channel: int) -> None:
    service = Service(tx_config(host, port))
    await burst(service, BASE_HZ, 50_000, 2.0)
    await asyncio.sleep(0.5)
    check("PTT low when not asked", read_gpio(host, user, channel) == 0, "")
    await wait_done(service)
    await burst(service, BASE_HZ, 50_000, 2.0, key_ptt=True)
    await asyncio.sleep(0.5)
    check("PTT high when asked", read_gpio(host, user, channel) == 1, "")
    await wait_done(service)
    await asyncio.sleep(0.2)
    check("PTT low after the burst", read_gpio(host, user, channel) == 0, "")

    # What the server does for a client that stops feeding, then dies, with no PTT low sent.
    control = Rx._role(host, port, 2)
    data = Rx._role(host, port, 3)
    control.sendall(WORD.pack(2 << 28))
    await asyncio.sleep(1.5)
    check(
        "stalled client leaves PTT high",
        read_gpio(host, user, channel) == 1,
        "server has no watchdog",
    )
    data.close()
    await asyncio.sleep(0.3)
    check("closing TX data alone leaves PTT high", read_gpio(host, user, channel) == 1, "")
    control.close()
    await asyncio.sleep(0.3)
    check("closing TX control lowers PTT", read_gpio(host, user, channel) == 0, "")


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--channel", type=int, choices=(1, 2), required=True)
    parser.add_argument("--i-own-this-idle-board", action="store_true")
    parser.add_argument("--i-cabled-out-to-in", action="store_true")
    parser.add_argument("--ssh-user", default="")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


async def main() -> int:
    args = parse()
    if not (args.i_own_this_idle_board and args.i_cabled_out_to_in):
        print(
            "Confirm the board is idle and OUT is cabled to IN, with no antenna.", file=sys.stderr
        )
        return 2
    port = PORTS[args.channel]
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex((args.host, port)) == 0:
            print("The SDR app is already running; refusing to take it over.", file=sys.stderr)
            return 2
    app(args.host, "start")
    try:
        await asyncio.sleep(1.5)
        for step in (
            every_rate,
            every_frequency,
            linearity,
            timing,
            splatter,
            stall,
            no_rf_at_default,
            second_client,
            killed_process,
            full_duplex,
            through_mcp,
        ):
            await step(args.host, port)
        if args.ssh_user and shutil.which("ssh"):
            await ptt(args.host, port, args.ssh_user, args.channel)
        else:
            print("NOT RUN  PTT checks need --ssh-user", flush=True)
    finally:
        app(args.host, "stop")
    passed = sum(bool(item["pass"]) for item in RESULTS)
    print(f"{passed} of {len(RESULTS)} checks passed")
    if args.report:
        args.report.write_text(json.dumps(RESULTS, indent=1))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
