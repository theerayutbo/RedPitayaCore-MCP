"""Fixed registry of built-in TX waveforms. Tool input only picks a name and bounded integers.

There is no I/Q upload, file path or expression: every sample comes from a generator in this
module, so what a burst can contain is reviewed here, not chosen by a caller.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Literal, cast, get_args

import numpy as np
import numpy.typing as npt

from .protocol import TX_BLOCK_SAMPLES, ramp_blocks

# aura_qpsk_v1, matching AURA's reference (src/aura/acquisition/synthetic_rf.py) bit for bit.
QPSK_SPS = 8
QPSK_RRC_BETA = 0.35
QPSK_RRC_SPAN = 8  # symbols
QPSK_PN_SEED = 0x5A
QPSK_PN_SYMBOLS = 16
QPSK_PN_REPETITIONS = 4
QPSK_RAMP_SYMBOLS = 8
QPSK_PAYLOAD_SEED = 0xA5

BURST_INDEX_MAX = 10_000
N_PAYLOAD_MIN, N_PAYLOAD_MAX = 16, 512
REPETITIONS_MAX = 100

WaveformName = Literal["aura_qpsk_v1"]

Complex = npt.NDArray[np.complex128]


@lru_cache(maxsize=1)
def _rrc_taps() -> npt.NDArray[np.float64]:
    """Root-raised-cosine taps with unit energy. Kept as a scalar loop, like the reference,
    so every tap is computed by the same operations in the same order."""
    sps, beta, span = QPSK_SPS, QPSK_RRC_BETA, QPSK_RRC_SPAN
    n = np.arange(-span * sps // 2, span * sps // 2 + 1) / sps
    taps = np.empty_like(n)
    for i, t in enumerate(n):
        if np.isclose(t, 0.0):
            taps[i] = 1 - beta + 4 * beta / np.pi
        elif np.isclose(abs(t), 1 / (4 * beta)):
            taps[i] = (
                beta
                / np.sqrt(2)
                * (
                    (1 + 2 / np.pi) * np.sin(np.pi / (4 * beta))
                    + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta))
                )
            )
        else:
            taps[i] = (
                np.sin(np.pi * t * (1 - beta)) + 4 * beta * t * np.cos(np.pi * t * (1 + beta))
            ) / (np.pi * t * (1 - (4 * beta * t) ** 2))
    unit: npt.NDArray[np.float64] = taps / np.sqrt(np.sum(taps**2))
    unit.setflags(write=False)
    return unit


def rrc_taps() -> npt.NDArray[np.float64]:
    """A copy of the aura_qpsk_v1 shaping filter (65 taps, unit energy)."""
    return _rrc_taps().copy()


def qpsk(rng: np.random.Generator, n: int) -> Complex:
    b = rng.integers(0, 2, size=(n, 2))
    symbols: Complex = ((1 - 2 * b[:, 0]) + 1j * (1 - 2 * b[:, 1])) / np.sqrt(2)
    return symbols


def pn_symbols() -> Complex:
    """The known 16-symbol PN preamble, shared by every burst."""
    return qpsk(np.random.default_rng(QPSK_PN_SEED), QPSK_PN_SYMBOLS)


def ramp_symbols() -> Complex:
    """Eight symbols alternating (1+1j)/sqrt(2) and (-1-1j)/sqrt(2), starting with the first."""
    ramp: Complex = np.tile(np.array([1 + 1j, -1 - 1j]) / np.sqrt(2), QPSK_RAMP_SYMBOLS // 2)
    return ramp


def payload_symbols(burst_index: int, n_payload: int) -> Complex:
    return qpsk(np.random.default_rng([QPSK_PAYLOAD_SEED, burst_index]), n_payload)


def burst_symbols(burst_index: int, n_payload: int) -> Complex:
    symbols: Complex = np.concatenate(
        [
            ramp_symbols(),
            np.tile(pn_symbols(), QPSK_PN_REPETITIONS),
            payload_symbols(burst_index, n_payload),
        ]
    )
    return symbols


def modulate(symbols: Complex) -> Complex:
    up = np.zeros(len(symbols) * QPSK_SPS, dtype=complex)
    up[::QPSK_SPS] = symbols
    return cast(Complex, np.convolve(up, _rrc_taps(), mode="full"))


def sha256_complex64(samples: npt.NDArray[np.complex64]) -> str:
    """SHA-256 of little-endian complex64 (float32 I, float32 Q), the bytes the DAC is sent."""
    return hashlib.sha256(samples.astype("<c8").tobytes()).hexdigest()


@dataclass(frozen=True)
class Waveform:
    """One burst of a built-in waveform, scaled so its peak magnitude is the amplitude."""

    name: str
    samples: npt.NDArray[np.complex64]
    samples_per_symbol: int
    details: Mapping[str, Any]


def _require_int(name: str, value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _aura_qpsk_v1(amplitude: float, burst_index: int, n_payload: int) -> Waveform:
    if type(amplitude) not in {int, float} or not (math.isfinite(amplitude) and 0 < amplitude <= 1):
        raise ValueError("Amplitude must be a finite fraction of full scale, above 0, at most 1")
    _require_int("burst_index", burst_index, 0, BURST_INDEX_MAX)
    _require_int("n_payload", n_payload, N_PAYLOAD_MIN, N_PAYLOAD_MAX)
    symbols = burst_symbols(burst_index, n_payload)
    shaped = modulate(symbols)
    peak = float(np.max(np.abs(shaped)))
    samples = (shaped * (amplitude / peak)).astype(np.complex64)
    samples.setflags(write=False)
    sent = samples.astype(np.complex128)
    return Waveform(
        name="aura_qpsk_v1",
        samples=samples,
        samples_per_symbol=QPSK_SPS,
        details={
            "modulation": "QPSK: (1-2*b0 + 1j*(1-2*b1)) / sqrt(2), bits from numpy default_rng",
            "pulse_shape": "root-raised-cosine, unit-energy taps",
            "samples_per_symbol": QPSK_SPS,
            "rrc_beta": QPSK_RRC_BETA,
            "rrc_span_symbols": QPSK_RRC_SPAN,
            "rrc_taps": len(_rrc_taps()),
            "ramp_symbols": QPSK_RAMP_SYMBOLS,
            "pn_symbols": QPSK_PN_SYMBOLS,
            "pn_repetitions": QPSK_PN_REPETITIONS,
            "pn_seed": QPSK_PN_SEED,
            "payload_seed": [QPSK_PAYLOAD_SEED, burst_index],
            "burst_index": burst_index,
            "n_payload": n_payload,
            "burst_symbols": len(symbols),
            "burst_samples": len(samples),
            "peak_magnitude": float(np.max(np.abs(sent))),
            "rms_magnitude": math.sqrt(float(np.mean(sent.real**2 + sent.imag**2))),
            "baseband_sha256": sha256_complex64(samples),
            "unscaled_sha256": sha256_complex64(shaped.astype(np.complex64)),
            "sha256_format": "little-endian complex64 (float32 I, float32 Q) of one burst; "
            "baseband_sha256 is after peak scaling (what is sent), unscaled_sha256 is the "
            "RRC-filtered symbols before scaling",
        },
    )


@dataclass(frozen=True)
class WaveformSpec:
    name: str
    description: str
    samples_per_symbol: int
    # Half of the nominal occupied bandwidth, as a fraction of the sample rate.
    half_bandwidth_fraction: float
    generate: Callable[[float, int, int], Waveform]


# Read-only at runtime. A new waveform needs a code change and review, never tool input.
WAVEFORMS: Mapping[str, WaveformSpec] = MappingProxyType(
    {
        "aura_qpsk_v1": WaveformSpec(
            name="aura_qpsk_v1",
            description="8 ramp symbols, 4 x 16-symbol PN preamble (seed 0x5A), QPSK payload "
            "(seed [0xA5, burst_index]); RRC beta 0.35, 8 samples per symbol, span 8 symbols",
            samples_per_symbol=QPSK_SPS,
            half_bandwidth_fraction=(1 + QPSK_RRC_BETA) / (2 * QPSK_SPS),
            generate=_aura_qpsk_v1,
        )
    }
)
if set(get_args(WaveformName)) != set(WAVEFORMS):
    raise RuntimeError("WaveformName must list exactly the registered waveforms")


def waveform_spec(name: object) -> WaveformSpec:
    if type(name) is not str or name not in WAVEFORMS:
        raise ValueError(f"Unknown waveform; built-in waveforms are {sorted(WAVEFORMS)}")
    return WAVEFORMS[name]


class BurstStream:
    """Repeated bursts separated by silent gaps, cut into whole 2048-sample TX blocks on demand.

    Nothing is materialised beyond one burst, so long gaps cost no memory. The last block is
    padded with zeros to the block boundary.
    """

    def __init__(
        self, burst: npt.NDArray[np.complex64], sample_rate: int, repetitions: int, gap: int
    ):
        if burst.dtype != np.complex64 or burst.ndim != 1 or len(burst) < 1:
            raise ValueError("A burst must be a non-empty one-dimensional complex64 array")
        _require_int("repetitions", repetitions, 1, REPETITIONS_MAX)
        if type(gap) is not int or gap < 0:
            raise ValueError("The gap must be a whole number of samples, at least 0")
        self.burst = burst
        self.sample_rate = sample_rate
        self.repetitions = repetitions
        self.gap = gap
        self.period = len(burst) + gap
        self.active_samples = repetitions * len(burst) + (repetitions - 1) * gap
        self.blocks = math.ceil(self.active_samples / TX_BLOCK_SAMPLES)
        self.padding_samples = self.blocks * TX_BLOCK_SAMPLES - self.active_samples
        self._silence = bytes(TX_BLOCK_SAMPLES * 8)

    def samples(self, index: int) -> npt.NDArray[np.complex64]:
        if type(index) is not int or not 0 <= index < self.blocks:
            raise IndexError("Block index outside the burst")
        n = np.arange(index * TX_BLOCK_SAMPLES, (index + 1) * TX_BLOCK_SAMPLES)
        offset = n % self.period
        inside = (n < self.active_samples) & (offset < len(self.burst))
        out = np.zeros(TX_BLOCK_SAMPLES, dtype=np.complex64)
        out[inside] = self.burst[offset[inside]]
        return out

    def block(self, index: int) -> bytes:
        """The block as sent: no edge shaping, since the waveform is the measurement."""
        out = self.samples(index)
        if not out.any():
            return self._silence
        return out.astype("<c8").tobytes()

    def falling(self, index: int) -> bytes:
        """The block with a falling edge, for a stop before the end."""
        return ramp_blocks(self.block(index), self.sample_rate)[1]
