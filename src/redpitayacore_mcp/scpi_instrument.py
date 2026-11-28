"""Bounded, opt-in SCPI instrument backend for STEMlab 125-14.

Separate from the SDR server. No app/FPGA switching, arbitrary SCPI, or
persistent continuous output. Operator must start official SCPI manually.
"""
from __future__ import annotations

import math
import socket
import time
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from .config import Settings

FS = 125_000_000
N = 4096
DECIMATIONS = tuple(2**i for i in range(17))
Units = Literal["RAW", "VOLTS"]
Gain = Literal["LV", "HV"]


@dataclass(frozen=True)
class Step:
    frequency_hz: int
    decimation: int
    cycles: int

    @property
    def sample_rate_hz(self) -> float:
        return FS / self.decimation

    @property
    def duration_s(self) -> float:
        return self.cycles / self.frequency_hz


def plan(frequencies: list[int]) -> list[Step]:
    """Plan finite bursts with >=16 captured cycles and >=6 samples/cycle."""
    if not 1 <= len(frequencies) <= 32 or len(set(frequencies)) != len(frequencies):
        raise ValueError("Supply 1 to 32 distinct frequencies")
    result = []
    for f in frequencies:
        if type(f) is not int or not 100 <= f <= 20_000_000:
            raise ValueError("Frequency must be an integer, 100 Hz to 20 MHz")
        dec = max(d for d in DECIMATIONS if d <= max(1, FS / (16 * f)))
        # Center-trigger buffer has 8192 post-trigger samples.
        cycles = math.ceil(8192 * f * dec / FS) + 16
        if cycles >= 65536 or N * f * dec / FS < 16:
            raise ValueError("Cannot acquire requested tone in bounded burst")
        result.append(Step(f, dec, cycles))
    return result


class SCPI:
    """Private CRLF ASCII transport with bounded replies and deadlines."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout)
        self.timeout = timeout
        self.buffer = bytearray()

    def close(self) -> None:
        self.sock.close()

    def send(self, command: str) -> None:
        self.sock.settimeout(self.timeout)
        self.sock.sendall((command + "\r\n").encode("ascii"))

    def query(self, command: str) -> str:
        self.send(command)
        deadline = time.monotonic() + self.timeout
        while b"\r\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("SCPI reply timeout")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(16384)
            if not chunk:
                raise ConnectionError("SCPI connection closed")
            self.buffer.extend(chunk)
            if len(self.buffer) > 1_048_576:
                raise ValueError("SCPI reply exceeds limit")
        line, _, rest = self.buffer.partition(b"\r\n")
        self.buffer = bytearray(rest)
        return line.decode("ascii").strip()


def parse_samples(reply: str, units: Units) -> NDArray[np.float64]:
    if not reply.startswith("{") or not reply.endswith("}"):
        raise ValueError("Invalid SCPI ASCII sample envelope")
    parts = reply[1:-1].split(",")
    if len(parts) != N:
        raise ValueError(f"Expected {N} samples, got {len(parts)}")
    data = np.array([float(p) for p in parts], dtype=np.float64)
    if not np.isfinite(data).all():
        raise ValueError("Nonfinite ADC data")
    if units == "RAW" and (np.any(data != np.trunc(data)) or np.any(abs(data) > 32768)):
        raise ValueError("Invalid ADC counts")
    return data


def _expect(scpi: SCPI, query: str, expected: str) -> None:
    if scpi.query(query).upper() != expected.upper():
        raise RuntimeError(f"SCPI readback mismatch: {query}")


def _number(scpi: SCPI, query: str, expected: float) -> None:
    if not math.isclose(float(scpi.query(query)), expected, rel_tol=1e-5, abs_tol=1e-8):
        raise RuntimeError(f"SCPI readback mismatch: {query}")


def _off(scpi: SCPI) -> None:
    scpi.send("OUTPUT1:STATE OFF")
    if scpi.query("OUTPUT1:STATE?").upper() not in {"0", "OFF"}:
        raise RuntimeError("OUT1 disable not confirmed; inspect board manually")


def _wait(scpi: SCPI, query: str, expected: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if scpi.query(query) == expected:
            return
        time.sleep(0.01)
    raise TimeoutError(f"SCPI wait expired: {query}")


def capture(
    config: Settings,
    *,
    port: int,
    decimation: int,
    units: Units,
    gain1: Gain,
    gain2: Gain,
    step: Step | None = None,
    amplitude_vpk: float = 0.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64], dict[str, Any]]:
    """One common trigger, one frozen buffer, both channels, no phase claims."""
    if decimation not in DECIMATIONS or units not in {"RAW", "VOLTS"}:
        raise ValueError("Unsupported acquisition setting")
    if gain1 not in {"LV", "HV"} or gain2 not in {"LV", "HV"}:
        raise ValueError("Invalid gain")
    if step is not None:
        if step not in plan([step.frequency_hz]) or step.decimation != decimation:
            raise ValueError("Invalid burst plan")
        if not math.isfinite(amplitude_vpk) or not 0 < amplitude_vpk <= 0.1:
            raise ValueError("Invalid peak voltage")
    if config.mode == "mock":
        t = np.arange(N) * decimation / FS
        f = step.frequency_hz if step else min(1000, FS / decimation / 16)
        amplitude = amplitude_vpk if step else 0.01
        a = amplitude * 0.12 * np.sin(2 * np.pi * f * t - 0.3)
        b = amplitude * 0.8 * np.sin(2 * np.pi * f * t)
        if units == "RAW":
            a, b = np.round(a * 8192), np.round(b * 8192)
        return a, b, {"mode": "mock", "trigger": "synthetic", "timestamp_source": "synthetic"}
    if not config.host or not config.allow_rx:
        raise PermissionError("Live SCPI capture requires configured host and RP_ALLOW_RX")
    if step:
        if (not config.allow_tx or config.tx_min_hz is None or config.tx_max_hz is None
                or not config.tx_min_hz <= step.frequency_hz <= config.tx_max_hz
                or step.duration_s > config.tx_max_seconds):
            raise PermissionError("Burst outside operator TX permission/band/duration")
    scpi = SCPI(config.host, port, config.timeout)
    try:
        # Exclusive official scope/SCPI ownership must be established externally.
        if step:
            _off(scpi)
            scpi.send("GEN:RST")
            _off(scpi)
            for cmd in (
                "SOUR1:FUNC SINE", f"SOUR1:FREQ:FIX {step.frequency_hz}",
                f"SOUR1:VOLT {amplitude_vpk:.9g}", "SOUR1:VOLT:OFFS 0",
                "SOUR1:TRig:SOUR INT", "SOUR1:BURS:STAT BURST",
                f"SOUR1:BURS:NCYC {step.cycles}", "SOUR1:BURS:NOR 1",
                "SOUR1:BURS:INITValue 0", "SOUR1:BURS:LASTValue 0",
                "SOUR1:BURS:USE:LASTSample OFF",
            ):
                scpi.send(cmd)
            _expect(scpi, "SOUR1:FUNC?", "SINE")
            _expect(scpi, "SOUR1:TRig:SOUR?", "INT")
            _expect(scpi, "SOUR1:BURS:STAT?", "BURST")
            _number(scpi, "SOUR1:FREQ:FIX?", step.frequency_hz)
            _number(scpi, "SOUR1:VOLT?", amplitude_vpk)
            _number(scpi, "SOUR1:VOLT:OFFS?", 0)
            _number(scpi, "SOUR1:BURS:NCYC?", step.cycles)
            _number(scpi, "SOUR1:BURS:NOR?", 1)
            _number(scpi, "SOUR1:BURS:INITValue?", 0)
            _number(scpi, "SOUR1:BURS:LASTValue?", 0)
            if scpi.query("SOUR1:BURS:USE:LASTSample?").upper() not in {"0", "OFF"}:
                raise RuntimeError("Generator last sample mode not disabled")
        scpi.send("ACQ:RST")
        for cmd in (
            f"ACQ:DEC {decimation}", "ACQ:AVG ON", "ACQ:TRig:DLY 0",
            f"ACQ:SOUR1:GAIN {gain1}", f"ACQ:SOUR2:GAIN {gain2}",
            f"ACQ:DATA:Units {units}", "ACQ:DATA:FORMAT ASCII",
        ):
            scpi.send(cmd)
        _number(scpi, "ACQ:DEC?", decimation)
        _number(scpi, "ACQ:TRig:DLY?", 0)
        _expect(scpi, "ACQ:SOUR1:GAIN?", gain1)
        _expect(scpi, "ACQ:SOUR2:GAIN?", gain2)
        _expect(scpi, "ACQ:DATA:Units?", units)
        _expect(scpi, "ACQ:DATA:FORMAT?", "ASCII")
        scpi.send("ACQ:START")
        time.sleep(8192 * decimation / FS + 0.02)
        if step:
            scpi.send("ACQ:TRig AWG_PE")
            scpi.send("OUTPUT1:STATE ON")
            before = time.monotonic_ns()
            scpi.send("SOUR1:TRig:INT")
        else:
            before = time.monotonic_ns()
            scpi.send("ACQ:TRig NOW")
        wait_s = config.timeout + 8192 * decimation / FS
        _wait(scpi, "ACQ:TRig:STAT?", "TD", wait_s)
        after = time.monotonic_ns()
        _wait(scpi, "ACQ:TRig:FILL?", "1", wait_s)
        scpi.send("ACQ:STOP")
        if step:
            _off(scpi)
        a = parse_samples(scpi.query(f"ACQ:SOUR1:DATA:TRig? {N},POST_TRIG"), units)
        b = parse_samples(scpi.query(f"ACQ:SOUR2:DATA:TRig? {N},POST_TRIG"), units)
        return a, b, {"mode": "live", "trigger": "AWG_PE" if step else "NOW",
                      "trigger_host_bracket_ns": [before, after],
                      "timestamp_source": "host_command_and_poll_not_hardware_timestamp"}
    finally:
        try:
            if step:
                _off(scpi)
        finally:
            try:
                scpi.send("ACQ:STOP")
            finally:
                scpi.close()
