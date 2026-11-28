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
from .scpi_instrument import FS, N, Gain, Units, SCPI, _off, capture, plan


def build_server(config: Settings) -> FastMCP[Any]:
    mcp = FastMCP(
        "redpitaya_instrument_mcp",
        instructions="Exclusive SCPI instrument session. Default mock; no FPGA switching. "
        "Live requires explicit operator confirmation of the physical input range, "
        "exclusive application ownership and safe OUT1 load before excitation.",
    )
    lock = threading.Lock()
    stop_latch = threading.Event()
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
            "stop_latched": stop_latch.is_set(),
            "hardware_validated": False, "hardware_output_state": "unknown_not_read",
        }

    @mcp.tool(annotations=changes)
    def rp_instrument_stop(
        acknowledge_exclusive_scope_and_inputs: bool = False,
    ) -> dict[str, Any]:
        """Latch stop for this server lifetime; no new bursts after latch.

        If busy, the active finite operation must finish/clean up. Retry
        when idle to verify output OFF. This is not a hardware emergency stop.
        """
        check_live(acknowledge_exclusive_scope_and_inputs)
        stop_latch.set()
        if not lock.acquire(blocking=False):
            return {"mode": config.mode, "stop_latched": True,
                    "output_off_confirmed": False, "status": "WAIT_FINITE_WINDOW"}
        try:
            if config.mode == "mock":
                return {"mode": "mock", "stop_latched": True,
                        "output_off_confirmed": False, "status": "MOCK_STOPPED"}
            if not config.host:
                raise PermissionError("Configured board required")
            scpi = SCPI(config.host, port, config.timeout)
            try:
                _off(scpi)
                scpi.send("ACQ:STOP")
                return {"mode": "live", "stop_latched": True,
                        "output_off_confirmed": True, "status": "SCPI_OFF_READBACK",
                        "physical_rf_absence_verified": False}
            finally:
                scpi.close()
        finally:
            lock.release()

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
            if stop_latch.is_set():
                raise RuntimeError("Instrument stop is latched; restart only after operator review")
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
                if stop_latch.is_set():
                    return {"sweep_id": sweep_id, "records": records, "complete": False, "status": "STOP_LATCHED"}
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

    @mcp.tool(annotations=changes)
    def rp_scan_windows(
        frequencies_hz: list[int],
        max_windows: int = 30,
        interval_s: float = 0.1,
        amplitude_vpk: float = 0.01,
    ) -> dict[str, Any]:
        """Bounded mock-only stream of finite capture windows, round-robin frequency.

        Writes each NPZ immediately. No live RF; live continuous capture requires
        independent timing, energy budget and cancellation validation.
        """
        if config.mode != "mock":
            raise PermissionError("Continuous hardware capture is not qualified")
        steps = plan(frequencies_hz)
        if type(max_windows) is not int or not 1 <= max_windows <= 300:
            raise ValueError("max_windows must be 1 to 300")
        if not math.isfinite(interval_s) or not 0.05 <= interval_s <= 1:
            raise ValueError("interval_s must be 0.05 to 1")
        if not math.isfinite(amplitude_vpk) or not 0 < amplitude_vpk <= max_vpk:
            raise ValueError("Invalid amplitude")
        if not lock.acquire(blocking=False):
            raise RuntimeError("Instrument already busy")
        scan_id = uuid.uuid4().hex
        records = []
        try:
            for i in range(max_windows):
                if stop_latch.is_set():
                    return {"scan_id": scan_id, "records": records, "complete": False, "mode": "mock", "status": "STOP_LATCHED"}
                step = steps[i % len(steps)]
                a, b, evidence = capture(
                    config, port=port, decimation=step.decimation, units="VOLTS",
                    gain1="LV", gain2="LV", step=step, amplitude_vpk=amplitude_vpk,
                )
                # Mock data has synthetic timing; wait until its nominal end.
                remaining = (evidence["synthetic_end_ns"] - time.monotonic_ns()) / 1e9
                time.sleep(max(0.0, remaining))
                records.append(save(a, b, {
                    **evidence, "schema_version": "1.0", "capture_id": uuid.uuid4().hex,
                    "sweep_id": scan_id, "clock_id": clock, "window_index": i,
                    "completed_host_ns": time.monotonic_ns(),
                    "sample_rate_hz": step.sample_rate_hz, "decimation": step.decimation,
                    "frequency_hz": step.frequency_hz, "units": "VOLTS",
                    "gain1": "LV", "gain2": "LV", "samples_per_channel": N,
                    "requested_amplitude_vpk": amplitude_vpk, "burst_cycles": step.cycles,
                    "averaging": True, "instrument_calibration_verified": False,
                }))
                if i + 1 < max_windows:
                    time.sleep(interval_s)
            return {"scan_id": scan_id, "records": records, "complete": True, "mode": "mock"}
        finally:
            lock.release()

    return mcp


def main() -> None:
    build_server(Settings.from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()
