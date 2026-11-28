"""Separate local MCP entry point for official SCPI instrumentation."""
from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import Settings
from .scpi_instrument import FS, N, Gain, Units, capture, plan


def build_server(config: Settings) -> FastMCP[Any]:
    mcp = FastMCP(
        "redpitaya_instrument_mcp",
        instructions="Exclusive SCPI instrument session. Default mock; no FPGA switching. "
        "Live requires explicit operator confirmation of the physical input range, "
        "exclusive application ownership and safe OUT1 load before excitation.",
    )
    lock = threading.Lock()
    root = Path(os.getenv("RP_INSTRUMENT_OUTPUT_DIR", "./captures")).resolve()
    port = int(os.getenv("RP_SCPI_PORT", "5000"))
    clock = os.getenv("RP_INSTRUMENT_CLOCK_ID") or "local-" + uuid.uuid4().hex
    live_allowed = os.getenv("RP_ALLOW_SCPI") == "true"
    max_vpk = float(os.getenv("RP_SCPI_MAX_VPK", "0.05"))
    if not 1 <= port <= 65535 or not math.isfinite(max_vpk) or not 0 < max_vpk <= 0.1:
        raise ValueError("Invalid SCPI configuration")
    changes = ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
    )

    def check_live(ack: bool) -> None:
        if config.mode == "live" and not (live_allowed and config.allow_rx and ack is True):
            raise PermissionError("Live requires RP_ALLOW_SCPI, RP_ALLOW_RX and acknowledgement")

    def save(a: Any, b: Any, meta: dict[str, Any]) -> dict[str, Any]:
        root.mkdir(parents=True, exist_ok=True)
        path = root / (str(meta["capture_id"]) + ".npz")
        with path.open("xb") as out:
            np.savez_compressed(out, in1=a, in2=b, metadata=json.dumps(meta, allow_nan=False))
        return {"path": str(path), "metadata": meta}

    @mcp.tool()
    def rp_acquisition_status() -> dict[str, Any]:
        """Local configuration only, not board readiness or RF state."""
        return {
            "mode": config.mode, "live_scpi_enabled": live_allowed,
            "allow_rx": config.allow_rx, "allow_tx": config.allow_tx,
            "clock_id": clock, "busy": lock.locked(), "max_vpk": max_vpk,
            "hardware_validated": False, "hardware_output_state": "unknown_not_read",
        }

    @mcp.tool()
    def rp_frequency_sweep_plan(frequencies_hz: list[int]) -> dict[str, Any]:
        """Offline plan; no board I/O."""
        steps = plan(frequencies_hz)
        return {"steps": [
            {"frequency_hz": s.frequency_hz, "decimation": s.decimation,
             "sample_rate_hz": s.sample_rate_hz, "burst_cycles": s.cycles,
             "burst_duration_s": s.duration_s, "samples_per_channel": N}
            for s in steps
        ]}

    @mcp.tool(annotations=changes)
    def rp_adc_capture(
        decimation: int = 1, units: Units = "VOLTS",
        gain1: Gain = "LV", gain2: Gain = "LV",
        acknowledge_exclusive_scope_and_inputs: bool = False,
    ) -> dict[str, Any]:
        """Capture both fast ADC channels on common trigger; no generator commands."""
        check_live(acknowledge_exclusive_scope_and_inputs)
        if not lock.acquire(blocking=False):
            raise RuntimeError("Instrument already busy")
        try:
            a, b, evidence = capture(
                config, port=port, decimation=decimation, units=units,
                gain1=gain1, gain2=gain2,
            )
            return save(a, b, {
                **evidence, "schema_version": "1.0", "capture_id": uuid.uuid4().hex,
                "clock_id": clock, "completed_host_ns": time.monotonic_ns(),
                "sample_rate_hz": FS / decimation, "decimation": decimation,
                "frequency_hz": None, "units": units, "gain1": gain1, "gain2": gain2,
                "samples_per_channel": N, "averaging": True,
                "instrument_calibration_verified": False,
            })
        finally:
            lock.release()

    @mcp.tool(annotations=changes)
    def rp_frequency_sweep(
        frequencies_hz: list[int], amplitude_vpk: float = 0.01,
        units: Units = "VOLTS", gain1: Gain = "LV", gain2: Gain = "LV",
        acknowledge_exclusive_scope_and_inputs: bool = False,
        acknowledge_rf_output: bool = False,
    ) -> dict[str, Any]:
        """Bounded stepped sine with one finite burst and common ADC trigger per frequency.

        User must confirm OUT1 load, input protection, physical jumpers and exclusive scope
        ownership before live use. No continuous generator mode or automatic app switching.
        """
        check_live(acknowledge_exclusive_scope_and_inputs)
        steps = plan(frequencies_hz)
        if not math.isfinite(amplitude_vpk) or not 0 < amplitude_vpk <= max_vpk:
            raise ValueError("Amplitude exceeds operator voltage ceiling")
        if config.mode == "live":
            if not config.allow_tx or acknowledge_rf_output is not True:
                raise PermissionError("Live sweep requires RP_ALLOW_TX and RF acknowledgement")
            if config.tx_min_hz is None or config.tx_max_hz is None:
                raise PermissionError("Operator TX band required")
            if any(not config.tx_min_hz <= s.frequency_hz <= config.tx_max_hz for s in steps):
                raise PermissionError("Frequency outside operator band")
            if sum(s.duration_s for s in steps) > config.tx_max_seconds:
                raise PermissionError("Total requested burst airtime exceeds operator ceiling")
        if not lock.acquire(blocking=False):
            raise RuntimeError("Instrument already busy")
        sweep_id = uuid.uuid4().hex
        records = []
        try:
            for step in steps:
                a, b, evidence = capture(
                    config, port=port, decimation=step.decimation, units=units,
                    gain1=gain1, gain2=gain2, step=step, amplitude_vpk=amplitude_vpk,
                )
                records.append(save(a, b, {
                    **evidence, "schema_version": "1.0", "capture_id": uuid.uuid4().hex,
                    "sweep_id": sweep_id, "clock_id": clock,
                    "completed_host_ns": time.monotonic_ns(),
                    "sample_rate_hz": step.sample_rate_hz, "decimation": step.decimation,
                    "frequency_hz": step.frequency_hz, "units": units,
                    "gain1": gain1, "gain2": gain2, "samples_per_channel": N,
                    "requested_amplitude_vpk": amplitude_vpk, "burst_cycles": step.cycles,
                    "averaging": True, "instrument_calibration_verified": False,
                }))
            return {"sweep_id": sweep_id, "records": records, "complete": True}
        finally:
            lock.release()

    return mcp


def main() -> None:
    build_server(Settings.from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()
