"""Bounded adapter for official Red Pitaya SCPI; no custom hardware driver."""

from __future__ import annotations

import asyncio
import socket
from typing import Any, Literal

from .config import Settings
from .transport import _close_writer

SCPI_PORT = 5000
MAX_LINE = 1024
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
        "available_tools": ["identity", "acquisition_status"],
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
                writer.write(command.encode("ascii") + b"\r\n")
                await writer.drain()
                try:
                    raw = await reader.readuntil(b"\r\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
                    raise ValueError("Incomplete or oversized SCPI response") from error
                if len(raw) > MAX_LINE:
                    raise ValueError("Oversized SCPI response")
                response = raw[:-2].decode("ascii", errors="strict").strip()
                if not response or any(not 32 <= ord(char) <= 126 for char in response):
                    raise ValueError("Empty or non-printable SCPI response")
                if command == "*IDN?":
                    _validate_identity(response)
                responses.append(response)
        return tuple(responses)
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
