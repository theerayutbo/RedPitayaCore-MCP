"""Fixed RX and TX words of pavel-demin/red-pitaya-notes at 71c3402a.

Words and interleaved float32 I/Q are little-endian on the target Zynq ARM.
No generic command encoder or register access exists.
"""

from __future__ import annotations

import math
import struct
from array import array

import numpy as np

RATES = (20_000, 50_000, 100_000, 250_000, 500_000, 1_250_000)
MAX_SAMPLES = 8192
TX_BLOCK_SAMPLES = 2048
PTT_HIGH = struct.pack("<I", 2 << 28)
PTT_LOW = struct.pack("<I", 3 << 28)
# Raised-cosine edge of a burst, capped at one block. A hard on or off measured -32 dBc of
# splatter next to the tone, against -67 dBc in the steady state.
TX_RAMP_S = 0.005


def rx_role(role: str) -> bytes:
    if role not in {"control", "data"}:
        raise ValueError("Only RX control and RX data roles exist here")
    return struct.pack("<I", 0 if role == "control" else 1)


def tx_role(role: str) -> bytes:
    if role not in {"control", "data"}:
        raise ValueError("Only TX control and TX data roles exist here")
    return struct.pack("<I", 2 if role == "control" else 3)


def rx_frequency(frequency_hz: int) -> bytes:
    if type(frequency_hz) is not int or not 1 <= frequency_hz <= 60_000_000:
        raise ValueError("RX frequency must be an integer from 1 to 60000000 Hz")
    return struct.pack("<I", frequency_hz)


def rx_rate(sample_rate: int) -> bytes:
    if type(sample_rate) is not int or sample_rate not in RATES:
        raise ValueError(f"RX sample rate must be one of {RATES}")
    return struct.pack("<I", (1 << 28) | RATES.index(sample_rate))


def tx_frequency(frequency_hz: int) -> bytes:
    if type(frequency_hz) is not int or not 1 <= frequency_hz <= 60_000_000:
        raise ValueError("TX frequency must be an integer from 1 to 60000000 Hz")
    return struct.pack("<I", frequency_hz)


def tx_rate(sample_rate: int) -> bytes:
    if type(sample_rate) is not int or sample_rate not in RATES:
        raise ValueError(f"TX sample rate must be one of {RATES}")
    return struct.pack("<I", (1 << 28) | RATES.index(sample_rate))


def tone_block(sample_rate: int, offset_hz: float, amplitude: float) -> tuple[bytes, float]:
    """Return one 2048-sample block of a tone and the tone's true offset in Hz.

    The offset snaps to a whole number of cycles per block, so one block repeats with no phase
    step and no per-block work, which keeps 1.25 MS/s within reach of Python.
    """
    cycles = round(offset_hz * TX_BLOCK_SAMPLES / sample_rate)
    step = 2 * math.pi * cycles / TX_BLOCK_SAMPLES
    values = array("f")
    for index in range(TX_BLOCK_SAMPLES):
        values.append(amplitude * math.cos(step * index))
        values.append(amplitude * math.sin(step * index))
    return struct.pack(
        f"<{TX_BLOCK_SAMPLES * 2}f", *values
    ), cycles * sample_rate / TX_BLOCK_SAMPLES


def ramp_blocks(block: bytes, sample_rate: int) -> tuple[bytes, bytes, bytes]:
    """Return the block with a rising edge, with a falling edge, and with both.

    The falling edge ends on the block's last sample, so the burst still ends on a block boundary.
    """
    count = TX_BLOCK_SAMPLES
    length = min(count, max(1, round(TX_RAMP_S * sample_rate)))
    values = struct.unpack(f"<{count * 2}f", block)
    rise = [0.5 - 0.5 * math.cos(math.pi * index / length) for index in range(length)]
    up = rise + [1.0] * (count - length)
    down = up[::-1]
    both = [min(a, b) for a, b in zip(up, down, strict=True)]

    def shaped(gains: list[float]) -> bytes:
        out = []
        for index, gain in enumerate(gains):
            out.append(values[2 * index] * gain)
            out.append(values[2 * index + 1] * gain)
        return struct.pack(f"<{count * 2}f", *out)

    return shaped(up), shaped(down), shaped(both)


def decode_iq_array(raw: bytes) -> np.ndarray:
    """Return the samples as complex64, checked in C so a read does not hold the GIL for long."""
    if not raw or len(raw) % 8 or len(raw) > MAX_SAMPLES * 8:
        raise ValueError("Expected 1 to 8192 complete float32 I/Q sample pairs")
    values = np.frombuffer(raw, dtype="<f4")
    if not np.isfinite(values).all():
        raise ValueError("Non-finite IQ data: check the app and protocol compatibility")
    return values.astype(np.float32).view(np.complex64)


def decode_iq(raw: bytes) -> list[tuple[float, float]]:
    pairs = decode_iq_array(raw).view(np.float32).reshape(-1, 2).tolist()
    return [(i, q) for i, q in pairs]
