"""Bounded adapter for official Red Pitaya SCPI; no custom hardware driver."""

from __future__ import annotations

import asyncio
import math
import re
import socket
from typing import Any, Literal

from .config import Settings
from .transport import _close_writer

SCPI_PORT = 5000
MAX_LINE = 1024
MAX_WAVEFORM_LINE = 16384
MAX_WAVEFORM_SAMPLES = 256
Query = Literal["identity", "acquisition_status"]
_QUERIES: dict[Query, tuple[str, ...]] = {
    "identity": ("*IDN?", "SYST:VERS?"),
    "acquisition_status": ("*IDN?", "ACQ:DEC:Factor?", "ACQ:TRIG:STAT?", "ACQ:TRIG:FILL?"),
}
_MOCK: dict[Query, tuple[str, ...]] = {
    "identity": ("REDPITAYA,INSTR,MOCK,0", "mock-version"),
    "acquisition_status": ("REDPITAYA,INSTR,MOCK,0", "64", "WAIT", "0"),
}


def capabilities(config: Settings) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "scpi_live_enabled": config.mode == "live" and config.allow_scpi,
        "operator_profile_required": "official SCPI running with v0.94, exclusive ownership",
        "available_tools": ["identity", "acquisition_status", "waveform_read"],
        "scpi_port": SCPI_PORT,
        "custom_driver": False,
        "image_identity_verified": False,
        "app_auto_start": False,
        "raw_mmio": False,
        "planned_adapters": ["C/Python API", "streaming", "monitor"],
        "note": "Capability inventory, not proof of installed or running services.",
    }


async def _exchange(config: Settings, query: Query) -> tuple[str, ...]:
    if config.mode != "live" or config.host is None or not config.allow_scpi:
        raise PermissionError("Live SCPI transport requires explicit operator configuration")
    commands = _QUERIES[query]
    writer: asyncio.StreamWriter | None = None
    responses: list[str] = []
    try:
        async with asyncio.timeout(config.timeout):
            reader, writer = await asyncio.open_connection(
                config.host, SCPI_PORT, family=socket.AF_INET, limit=MAX_LINE
            )
            for command in commands:
                response = await _query_line(reader, writer, command, MAX_LINE)
                if command == "*IDN?":
                    _validate_identity(response)
                responses.append(response)
        return tuple(responses)
    finally:
        await _close_writer(writer)


async def _query_line(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    command: str,
    max_bytes: int,
) -> str:
    # Callers construct commands only from fixed literals and validated integers.
    writer.write(command.encode("ascii") + b"\r\n")
    await writer.drain()
    try:
        raw = await reader.readuntil(b"\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        raise ValueError("Incomplete or oversized SCPI response") from error
    if len(raw) > max_bytes:
        raise ValueError("Oversized SCPI response")
    response = raw[:-2].decode("ascii", errors="strict").strip()
    if not response or any(not 32 <= ord(char) <= 126 for char in response):
        raise ValueError("Empty or non-printable SCPI response")
    return response


def _waveform_values(response: str, sample_count: int) -> list[float]:
    if not response.startswith("{") or not response.endswith("}"):
        raise ValueError("Expected official ASCII waveform array")
    fields = response[1:-1].split(",")
    if len(fields) != sample_count:
        raise ValueError("Waveform sample count mismatch")
    values: list[float] = []
    for field in fields:
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", field):
            raise ValueError("Invalid waveform number")
        value = float(field)
        # Official ADC readings cannot approach this bound; also protects statistics.
        if not math.isfinite(value) or abs(value) > 1e6:
            raise ValueError("Non-finite or implausible waveform value")
        values.append(value)
    return values


async def _read_waveform(
    config: Settings, channel: int, sample_count: int
) -> tuple[float, list[float]]:
    if config.mode != "live" or config.host is None or not config.allow_scpi:
        raise PermissionError("Live SCPI transport requires explicit operator configuration")
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(config.timeout):
            reader, writer = await asyncio.open_connection(
                config.host, SCPI_PORT, family=socket.AF_INET, limit=MAX_WAVEFORM_LINE
            )

            async def query(command: str, limit: int = MAX_LINE) -> str:
                return await _query_line(reader, writer, command, limit)

            _validate_identity(await query("*IDN?"))
            # Never change global units, acquisition state, trigger or format.
            for command, expected in (
                ("ACQ:DATA:FORMAT?", "ASCII"),
                ("ACQ:DATA:UNITS?", "VOLTS"),
                ("ACQ:KEEP:ARM?", "0"),
                ("ACQ:TRIG:STAT?", "TD"),
                ("ACQ:TRIG:FILL?", "1"),
            ):
                if await query(command) != expected:
                    raise ValueError(f"Waveform precondition not met: {command}")
            rate_text = await query("ACQ:SRAT?")
            match = re.fullmatch(r"([0-9]+) +Hz", rate_text)
            if match is None:
                raise ValueError("Unexpected sampling rate response")
            rate = float(match.group(1))
            if not 0 < rate <= 125_000_000:
                raise ValueError("Sampling rate outside STEMlab 125-14 profile")
            raw = await query(f"ACQ:SOUR{channel}:DATA:OLD:N? {sample_count}", MAX_WAVEFORM_LINE)
            values = _waveform_values(raw, sample_count)
            if await query("ACQ:TRIG:STAT?") != "TD" or await query("ACQ:TRIG:FILL?") != "1":
                raise ValueError("Acquisition state changed during read")
            return rate, values
    finally:
        await _close_writer(writer)


def _validate_identity(identity: str) -> None:
    fields = identity.split(",")
    if len(fields) != 4 or fields[:2] != ["REDPITAYA", "INSTR"]:
        raise ValueError("Unexpected official SCPI identity response")


class OfficialSCPI:
    def __init__(self, config: Settings):
        self.config = config
        self.lock = asyncio.Lock()

    async def read(self, query: Query, acknowledge_exclusive_scpi: bool) -> dict[str, Any]:
        if query not in _QUERIES:
            raise ValueError("Unknown official SCPI query")
        live = self.config.mode == "live"
        if live and (not self.config.allow_scpi or acknowledge_exclusive_scpi is not True):
            raise PermissionError(
                "Set RP_ALLOW_SCPI=true and acknowledge_exclusive_scpi=true only after "
                "confirming the operator-started SCPI server, v0.94 image and exclusive access"
            )
        async with self.lock:
            values = await _exchange(self.config, query) if live else _MOCK[query]
        result: dict[str, Any] = {
            "mode": self.config.mode,
            "simulated": not live,
            "transport": "official_scpi",
            "image_identity_verified": False,
            "exclusive_access_verified": False,
            "operator_acknowledged": live and acknowledge_exclusive_scpi,
            "note": "Sequential readback, not an atomic snapshot or hardware identity proof.",
        }
        if query == "identity":
            identity, version = values
            _validate_identity(identity)
            return {**result, "identity": identity, "api_version_reported": version}
        identity, decimation, trigger, fill = values
        _validate_identity(identity)
        if not decimation.isascii() or not decimation.isdecimal() or len(decimation) > 5:
            raise ValueError("Unexpected decimation response")
        factor = int(decimation)
        if factor not in {1, 2, 4, 8} and not 16 <= factor <= 65536:
            raise ValueError("Decimation outside official extended range")
        if trigger not in {"WAIT", "TD"} or fill not in {"0", "1"}:
            raise ValueError("Unexpected trigger/fill response")
        return {
            **result,
            "identity": identity,
            "decimation": factor,
            "trigger_state": trigger,
            "buffer_full": fill == "1",
        }

    async def waveform(
        self, channel: int, sample_count: int, acknowledge_exclusive_scpi: bool
    ) -> dict[str, Any]:
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("Select channel 1 or 2")
        if type(sample_count) is not int or not 1 <= sample_count <= MAX_WAVEFORM_SAMPLES:
            raise ValueError("sample_count must be an integer from 1 to 256")
        live = self.config.mode == "live"
        if live and (not self.config.allow_scpi or acknowledge_exclusive_scpi is not True):
            raise PermissionError("Live waveform read requires SCPI permission and acknowledgement")
        async with self.lock:
            if live:
                rate, values = await _read_waveform(self.config, channel, sample_count)
            else:
                rate = 125_000_000 / 64
                values = [
                    0.25 * math.sin(2 * math.pi * i / 32 + (channel - 1) * math.pi / 2)
                    for i in range(sample_count)
                ]
        return {
            "mode": self.config.mode,
            "simulated": not live,
            "channel": channel,
            "sample_count": len(values),
            "sample_rate_hz": rate,
            "unit": "V",
            "samples": values,
            "minimum_v": min(values),
            "maximum_v": max(values),
            "mean_v": math.fsum(values) / len(values),
            "rms_v": math.sqrt(math.fsum(v * v for v in values) / len(values)),
            "acquisition_started": False,
            "image_identity_verified": False,
            "exclusive_access_verified": False,
            "note": "Oldest samples from an existing completed acquisition; "
            "not a new capture, atomic snapshot or independently calibrated measurement.",
        }
