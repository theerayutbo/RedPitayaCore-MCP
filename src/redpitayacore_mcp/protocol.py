"""Only the RX subset of pavel-demin/red-pitaya-notes at 71c3402a.

Words and interleaved float32 I/Q are little-endian on the target Zynq ARM.
No generic command encoder, TX selector, PTT command, or register access exists.
"""

from __future__ import annotations

import math
import struct

RATES = (20_000, 50_000, 100_000, 250_000, 500_000, 1_250_000)
MAX_SAMPLES = 8192


def rx_role(role: str) -> bytes:
    if role not in {"control", "data"}:
        raise ValueError("Only RX control and RX data roles are supported; TX is disabled")
    return struct.pack("<I", 0 if role == "control" else 1)


def rx_frequency(frequency_hz: int) -> bytes:
    if type(frequency_hz) is not int or not 1 <= frequency_hz <= 60_000_000:
        raise ValueError("RX frequency must be an integer from 1 to 60000000 Hz")
    return struct.pack("<I", frequency_hz)


def rx_rate(sample_rate: int) -> bytes:
    if type(sample_rate) is not int or sample_rate not in RATES:
        raise ValueError(f"RX sample rate must be one of {RATES}")
    return struct.pack("<I", (1 << 28) | RATES.index(sample_rate))


def decode_iq(raw: bytes) -> list[tuple[float, float]]:
    if not raw or len(raw) % 8 or len(raw) > MAX_SAMPLES * 8:
        raise ValueError("Expected 1 to 8192 complete float32 I/Q sample pairs")
    samples = list(struct.iter_unpack("<ff", raw))
    if any(not math.isfinite(value) for sample in samples for value in sample):
        raise ValueError("Non-finite IQ data: check the app and protocol compatibility")
    return samples
