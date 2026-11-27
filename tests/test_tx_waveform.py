"""rp_tx_waveform: gates, the aura_qpsk_v1 spec, airtime accounting and the streaming path.

Hardware is never contacted: live-mode tests talk to the loopback fake board from test_tx.
"""

import asyncio
import dataclasses
import hashlib
import math
import struct

import numpy as np
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from test_tx import BLOCK, Board, board, forbid_sockets, live

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.protocol import (
    PTT_HIGH,
    PTT_LOW,
    TX_BLOCK_SAMPLES,
    tx_frequency,
    tx_rate,
)
from redpitayacore_mcp.server import build_server
from redpitayacore_mcp.service import Service
from redpitayacore_mcp.transport import (
    TX_FLUSH_BLOCKS,
    TX_PREROLL_BLOCKS,
    TX_START_SILENCE_S,
    TxBurst,
)
from redpitayacore_mcp.waveforms import (
    WAVEFORMS,
    BurstStream,
    burst_symbols,
    pn_symbols,
    ramp_symbols,
    rrc_taps,
    snap_to_grid,
    waveform_spec,
)

# burst_index=0, n_payload=96, amplitude=0.01 (any sample rate: samples do not depend on it).
SHA_0_96_001 = "ee0342c5e5bb41c3730d4af384f21e76585cabe1be81ff0713695a08a7074f9e"
# The same burst before peak scaling: AURA's own modulate() output, snapped to the grid.
UNSCALED_SHA_0_96 = "3b80cff4666d80cf4089244a8489c5f416f2c24aded5741e8e3cc0779e83bf43"
GRID = 2.0**-24

GOOD = {
    "waveform": "aura_qpsk_v1",
    "frequency_hz": 1_000_000,
    "sample_rate": 250_000,
    "amplitude": 0.01,
    "burst_index": 0,
    "n_payload": 96,
    "repetitions": 1,
    "gap_s": 0.0,
    "key_ptt": False,
    "acknowledge_rf_output": True,
}


def silent_blocks(rate: int) -> int:
    """Silent blocks before the first burst block: the gate's preroll, then the lead-in."""
    return TX_PREROLL_BLOCKS + math.ceil(TX_START_SILENCE_S * rate / TX_BLOCK_SAMPLES)


def blocks_of(data: bytes) -> list[bytes]:
    return [data[index : index + BLOCK] for index in range(0, len(data), BLOCK)]


def generate(amplitude: float = 0.01, burst_index: int = 0, n_payload: int = 96):
    return WAVEFORMS["aura_qpsk_v1"].generate(amplitude, burst_index, n_payload)


# --- The spec, written out again from its text, independent of the module under test. ---


def spec_taps() -> np.ndarray:
    sps, beta, span = 8, 0.35, 8
    t = np.arange(-span * sps // 2, span * sps // 2 + 1) / sps
    taps = np.empty_like(t)
    for i, x in enumerate(t):
        if x == 0:
            taps[i] = 1 - beta + 4 * beta / np.pi
        elif abs(x) == 1 / (4 * beta):
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
                np.sin(np.pi * x * (1 - beta)) + 4 * beta * x * np.cos(np.pi * x * (1 + beta))
            ) / (np.pi * x * (1 - (4 * beta * x) ** 2))
    return taps / np.sqrt(np.sum(taps**2))


def spec_qpsk(rng: np.random.Generator, n: int) -> np.ndarray:
    b = rng.integers(0, 2, size=(n, 2))
    return ((1 - 2 * b[:, 0]) + 1j * (1 - 2 * b[:, 1])) / np.sqrt(2)


def spec_symbols(burst_index: int, n_payload: int) -> np.ndarray:
    pn = spec_qpsk(np.random.default_rng(0x5A), 16)
    ramp = np.array([(1 + 1j) / np.sqrt(2), (-1 - 1j) / np.sqrt(2)] * 4)
    payload = spec_qpsk(np.random.default_rng([0xA5, burst_index]), n_payload)
    return np.concatenate([ramp, np.tile(pn, 4), payload])


def spec_scaled(burst_index: int, n_payload: int, amplitude: float) -> np.ndarray:
    """The float64 burst, as AURA's modulate() gives it, scaled to the peak."""
    symbols = spec_symbols(burst_index, n_payload)
    up = np.zeros(len(symbols) * 8, dtype=complex)
    up[::8] = symbols
    shaped = np.convolve(up, spec_taps(), mode="full")
    return shaped * (amplitude / np.max(np.abs(shaped)))


def spec_snap(values: np.ndarray) -> np.ndarray:
    """Multiples of 2**-24, truncated toward zero, -0.0 as 0.0, as complex64."""
    real = np.trunc(values.real * 2**24) / 2**24 + 0.0
    imag = np.trunc(values.imag * 2**24) / 2**24 + 0.0
    return (real + 1j * imag).astype(np.complex64)


def spec_burst(burst_index: int, n_payload: int, amplitude: float) -> np.ndarray:
    return spec_snap(spec_scaled(burst_index, n_payload, amplitude))


def test_rrc_taps_follow_the_spec():
    taps = rrc_taps()
    assert len(taps) == 65
    assert np.array_equal(taps, spec_taps())
    assert np.sum(taps**2) == pytest.approx(1.0, abs=1e-12)
    assert np.allclose(taps, taps[::-1], rtol=0, atol=1e-15)
    assert np.argmax(taps) == 32


def test_preamble_equals_the_spec():
    ramp = ramp_symbols()
    a, b = (1 + 1j) / np.sqrt(2), (-1 - 1j) / np.sqrt(2)
    assert ramp.tolist() == [a, b, a, b, a, b, a, b]
    pn = pn_symbols()
    assert np.array_equal(pn, spec_qpsk(np.random.default_rng(0x5A), 16))
    # Pinned bit pattern: a change in numpy's default generator would change AURA's preamble too.
    i_positive = [0, 1, 1, 1, 0, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1]
    q_positive = [1, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 0, 0, 1]
    assert (pn.real > 0).astype(int).tolist() == i_positive
    assert (pn.imag > 0).astype(int).tolist() == q_positive
    assert np.allclose(np.abs(pn), 1.0)
    symbols = burst_symbols(7, 32)
    assert len(symbols) == 8 + 4 * 16 + 32
    assert np.array_equal(symbols[:8], ramp)
    assert np.array_equal(symbols[8:72], np.tile(pn, 4))
    assert np.array_equal(symbols[72:], spec_qpsk(np.random.default_rng([0xA5, 7]), 32))


@pytest.mark.parametrize(("burst_index", "n_payload"), [(0, 96), (1, 16), (10_000, 512)])
def test_waveform_matches_the_spec_bit_for_bit(burst_index, n_payload):
    wave = generate(0.05, burst_index, n_payload)
    expected = spec_burst(burst_index, n_payload, 0.05)
    assert wave.samples.dtype == np.complex64
    assert len(wave.samples) == (8 + 64 + n_payload) * 8 + 64
    assert wave.samples.tobytes() == expected.tobytes()
    # The grid moves no sample by more than one step per component.
    deviation = np.abs(wave.samples - spec_scaled(burst_index, n_payload, 0.05))
    assert deviation.max() <= GRID * math.sqrt(2)


def test_sha_does_not_depend_on_float64_noise():
    """CI found machines whose BLAS and SIMD give float64 results a few ulp apart. Without the
    grid, cancelled terms near 1e-19 survive in float32 and change the SHA-256. Simulate other
    machines with another summation order and taps nudged by a few ulp."""
    rng = np.random.default_rng(1)
    symbols = burst_symbols(0, 96)
    taps = rrc_taps()
    up = np.zeros(len(symbols) * 8, dtype=complex)
    up[::8] = symbols
    variants = []
    for order in (range(len(symbols)), reversed(range(len(symbols)))):
        out = np.zeros(len(symbols) * 8 + 64, dtype=complex)
        for m in order:
            out[8 * m : 8 * m + 65] += symbols[m] * taps
        variants.append(out)
    for _ in range(8):
        nudged = taps.copy()
        for _ in range(int(rng.integers(1, 5))):
            nudged = np.nextafter(nudged, np.where(rng.random(len(taps)) < 0.5, np.inf, -np.inf))
        variants.append(np.convolve(up, nudged))
    raw, snapped = set(), set()
    for shaped in variants:
        scaled = shaped * (0.01 / np.max(np.abs(shaped)))
        raw.add(hashlib.sha256(scaled.astype("<c8").tobytes()).hexdigest())
        snapped.add(hashlib.sha256(snap_to_grid(scaled).tobytes()).hexdigest())
    assert len(raw) > 1  # the hazard is real
    assert snapped == {SHA_0_96_001}


def test_snap_to_grid_is_exact_never_grows_and_has_no_negative_zero():
    values = np.array([1e-19 - 1e-19j, -1e-19 + 0j, 0.3 + 0.7j, -0.25 - 0.9j, 1 + 0j])
    snapped = snap_to_grid(values)
    assert snapped.dtype == np.complex64
    parts = snapped.view(np.float32)
    assert not np.signbit(parts[parts == 0]).any()
    assert np.all((parts.astype(np.float64) / GRID) % 1 == 0)
    assert np.all(np.abs(snapped.astype(np.complex128)) <= np.abs(values))
    assert snapped[0] == 0 and snapped[4] == 1
    for bad in ([1.5 + 0j], [0.8 + 0.8j], [complex(math.nan, 0)], [complex(math.inf, 0)]):
        with pytest.raises(ValueError, match="full scale"):
            snap_to_grid(np.array(bad))


def test_expected_sha_for_fixed_parameters():
    wave = generate(0.01, 0, 96)
    assert wave.details["baseband_sha256"] == SHA_0_96_001
    assert wave.details["unscaled_sha256"] == UNSCALED_SHA_0_96
    assert hashlib.sha256(wave.samples.astype("<c8").tobytes()).hexdigest() == SHA_0_96_001
    assert wave.details["burst_samples"] == 1408
    assert wave.details["burst_symbols"] == 168


def test_waveform_is_deterministic_and_seeded_by_burst_index():
    first, again = generate(0.02, 5, 64), generate(0.02, 5, 64)
    assert first.samples.tobytes() == again.samples.tobytes()
    assert first.details["baseband_sha256"] == again.details["baseband_sha256"]
    other = generate(0.02, 6, 64)
    assert other.details["baseband_sha256"] != first.details["baseband_sha256"]
    # Only the payload changes: ramp and preamble symbols are the same in every burst.
    assert np.array_equal(burst_symbols(5, 64)[:72], burst_symbols(6, 64)[:72])
    assert not np.array_equal(burst_symbols(5, 64)[72:], burst_symbols(6, 64)[72:])


@pytest.mark.parametrize("amplitude", [0.001, 0.01, 0.05, 0.1, 1.0])
@pytest.mark.parametrize("burst_index", [0, 1, 9_999])
def test_peak_magnitude_equals_amplitude(amplitude, burst_index):
    wave = generate(amplitude, burst_index)
    magnitude = np.abs(wave.samples.astype(np.complex128))
    # Truncating toward zero on the grid never overshoots, and costs under 1.5 grid steps.
    assert amplitude - 1.5 * GRID <= magnitude.max() <= amplitude
    assert wave.details["peak_magnitude"] == magnitude.max()
    rms = math.sqrt(float(np.mean(magnitude**2)))
    assert wave.details["rms_magnitude"] == pytest.approx(rms, rel=1e-12)
    # The shape does not change with amplitude, so neither does the crest factor.
    assert rms / amplitude == pytest.approx(
        generate(1.0, burst_index).details["rms_magnitude"], rel=1e-3
    )
    if burst_index == 0:
        assert rms / amplitude == pytest.approx(0.6417778, rel=1e-3)


def test_matched_filter_recovers_the_symbols():
    wave = generate(0.05, 3, 96)
    symbols = burst_symbols(3, 96)
    received = np.convolve(wave.samples.astype(np.complex128), rrc_taps())
    # Two 65-tap filters delay by 64 samples; sample once per symbol from there.
    decided = received[64 : 64 + 8 * len(symbols) : 8]
    gain = np.vdot(symbols, decided) / np.vdot(symbols, symbols)
    error = np.abs(decided / gain - symbols)
    assert error.max() < 0.05


# --- Repetitions, gaps and airtime accounting. ---


def test_stream_is_bursts_then_gaps_then_zero_padding():
    burst = generate().samples
    stream = BurstStream(burst, 250_000, 3, 2500)
    assert stream.active_samples == 3 * 1408 + 2 * 2500
    assert stream.blocks == math.ceil(9224 / 2048) == 5
    assert stream.padding_samples == 5 * 2048 - 9224
    gap = np.zeros(2500, dtype=np.complex64)
    pad = np.zeros(stream.padding_samples, dtype=np.complex64)
    expected = np.concatenate([burst, gap, burst, gap, burst, pad]).astype("<c8").tobytes()
    assert b"".join(stream.block(index) for index in range(stream.blocks)) == expected
    with pytest.raises(IndexError):
        stream.block(stream.blocks)


def test_long_gaps_cost_no_memory():
    stream = BurstStream(generate().samples, 1_250_000, 100, 2_000_000)
    assert stream.blocks == math.ceil((100 * 1408 + 99 * 2_000_000) / 2048)
    # Deep inside the first gap: silence, built from one burst, nothing materialised.
    assert stream.block(10) == bytes(BLOCK)


async def test_airtime_counts_repetitions_gaps_and_padding():
    service = Service(Settings())
    result = await service.tx_waveform(**{**GOOD, "repetitions": 3, "gap_s": 0.01})
    wave = result["waveform"]
    assert wave["repetitions"] == 3
    assert wave["gap_samples"] == 2500 and wave["gap_s"] == 0.01
    assert wave["active_samples"] == 9224
    assert wave["active_s"] == pytest.approx(9224 / 250_000)
    assert wave["blocks"] == 5 and wave["padding_samples"] == 1016
    assert wave["stream_samples"] == 5 * 2048
    assert wave["duration_s"] == pytest.approx(5 * 2048 / 250_000)
    assert result["requested_settings"]["duration_s"] == wave["duration_s"]
    assert wave["burst_s"] == pytest.approx(1408 / 250_000)
    await service.tx_stop()


async def test_gap_rounds_to_whole_samples_and_reports_the_true_value():
    service = Service(Settings())
    result = await service.tx_waveform(**{**GOOD, "repetitions": 2, "gap_s": 0.0000041})
    assert result["waveform"]["gap_samples"] == 1
    assert result["waveform"]["gap_s"] == 1 / 250_000
    await service.tx_stop()


@pytest.mark.parametrize(("gap_s", "allowed"), [(0.084, True), (0.088, False)])
async def test_airtime_limit_includes_gaps_and_padding(monkeypatch, gap_s, allowed):
    """At 0.1 s and 250 kS/s, 12 whole blocks fit. Two bursts and a 21000-sample gap fill 11.6
    blocks; a 22000-sample gap needs a 13th block, so its zero padding breaks the limit."""
    forbid_sockets(monkeypatch)
    service = Service(Settings(tx_max_seconds=0.1))
    request = {**GOOD, "repetitions": 2, "gap_s": gap_s}
    if allowed:
        result = await service.tx_waveform(**request)
        assert result["waveform"]["blocks"] == 12
        assert result["waveform"]["duration_s"] <= 0.1
        await service.tx_stop()
    else:
        with pytest.raises(ValueError, match="Airtime"):
            await service.tx_waveform(**request)


async def test_padding_alone_can_exceed_the_limit(monkeypatch):
    """One 70 ms burst at 20 kS/s still occupies a whole 102 ms block."""
    forbid_sockets(monkeypatch)
    with pytest.raises(ValueError, match="Airtime"):
        await Service(Settings(tx_max_seconds=0.1)).tx_waveform(**{**GOOD, "sample_rate": 20_000})


# --- Gates. ---


async def test_live_waveform_needs_the_operator_switch(monkeypatch):
    forbid_sockets(monkeypatch)
    with pytest.raises(PermissionError, match="RP_ALLOW_TX"):
        await Service(live(allow_tx=False, tx_min_hz=None, tx_max_hz=None)).tx_waveform(**GOOD)


@pytest.mark.parametrize("acknowledgement", [False, None, 1, "true"])
async def test_live_waveform_needs_a_per_call_acknowledgement(monkeypatch, acknowledgement):
    forbid_sockets(monkeypatch)
    with pytest.raises(PermissionError, match="acknowledge_rf_output"):
        await Service(live()).tx_waveform(**{**GOOD, "acknowledge_rf_output": acknowledgement})


@pytest.mark.parametrize(
    ("change", "error"),
    [
        # Out of the operator band 900-1100 kHz.
        ({"frequency_hz": 2_000_000}, PermissionError),
        ({"frequency_hz": 800_000}, PermissionError),
        # The carrier is in band, but the occupied bandwidth (+-21.1 kHz at 250 kS/s) is not.
        ({"frequency_hz": 1_090_000}, PermissionError),
        ({"frequency_hz": 1_000_000, "sample_rate": 1_250_000}, PermissionError),
        # Amplitude above the operator limit, or not a positive finite number.
        ({"amplitude": 0.11}, ValueError),
        ({"amplitude": 0}, ValueError),
        ({"amplitude": -0.01}, ValueError),
        ({"amplitude": math.nan}, ValueError),
        ({"amplitude": math.inf}, ValueError),
        ({"amplitude": True}, ValueError),
        # Airtime over RP_TX_MAX_SECONDS (10 s): 99 gaps of 0.2 s.
        ({"repetitions": 100, "gap_s": 0.2}, ValueError),
        ({"gap_s": 10.5, "repetitions": 2}, ValueError),
        ({"gap_s": -0.001}, ValueError),
        ({"gap_s": math.nan}, ValueError),
        # Parameters outside their ranges.
        ({"burst_index": -1}, ValueError),
        ({"burst_index": 10_001}, ValueError),
        ({"burst_index": True}, ValueError),
        ({"burst_index": 1.0}, ValueError),
        ({"n_payload": 15}, ValueError),
        ({"n_payload": 513}, ValueError),
        ({"repetitions": 0}, ValueError),
        ({"repetitions": 101}, ValueError),
        ({"sample_rate": 48_000}, ValueError),
        ({"frequency_hz": 0}, ValueError),
    ],
)
async def test_limits_refuse_before_any_socket(monkeypatch, change, error):
    forbid_sockets(monkeypatch)
    service = Service(live())
    with pytest.raises(error):
        await service.tx_waveform(**{**GOOD, **change})
    assert service.tx_status()["requested_settings"] is None


@pytest.mark.parametrize(
    "name",
    ["aura_qpsk_v2", "AURA_QPSK_V1", " aura_qpsk_v1", "", "tone", None, 1, ["aura_qpsk_v1"]],
)
async def test_unknown_waveform_is_refused(monkeypatch, name):
    forbid_sockets(monkeypatch)
    for service in (Service(live()), Service(Settings())):
        with pytest.raises(ValueError, match="Unknown waveform"):
            await service.tx_waveform(**{**GOOD, "waveform": name})


async def test_mock_applies_the_band_when_one_is_set():
    service = Service(Settings(tx_min_hz=900_000, tx_max_hz=1_100_000))
    with pytest.raises(PermissionError):
        await service.tx_waveform(**{**GOOD, "frequency_hz": 1_090_000})


async def test_transport_also_refuses_without_the_switch(monkeypatch):
    forbid_sockets(monkeypatch)
    stream = BurstStream(generate().samples, 250_000, 1, 0)
    with pytest.raises(PermissionError):
        await TxBurst(live(allow_tx=False, tx_min_hz=None, tx_max_hz=None)).start_stream(
            1_000_000, 250_000, stream, False
        )


async def test_transport_also_refuses_an_over_long_stream(monkeypatch):
    forbid_sockets(monkeypatch)
    stream = BurstStream(generate().samples, 250_000, 2, 30_000)
    assert stream.blocks * 2048 / 250_000 > 0.1
    with pytest.raises(ValueError, match="at most"):
        await TxBurst(live(tx_max_seconds=0.1)).start_stream(1_000_000, 250_000, stream, False)


class Broken:
    """A block source whose block 3 has the wrong length."""

    blocks = 10

    def block(self, index: int) -> bytes:
        return bytes(100) if index == 3 else bytes(BLOCK)

    def falling(self, index: int) -> bytes:
        return bytes(BLOCK)


async def test_transport_refuses_a_bad_first_block(monkeypatch):
    forbid_sockets(monkeypatch)

    class BadStart(Broken):
        def block(self, index: int) -> bytes:
            return bytes(BLOCK - 8)

    with pytest.raises(ValueError, match="2048 samples"):
        await TxBurst(live()).start_stream(1_000_000, 50_000, BadStart(), False)


async def test_a_bad_block_mid_stream_ends_safely():
    """A failure while feeding drops the data socket but keeps control until the queue plays."""
    fake = Board()
    async with board(fake) as port:
        tx = TxBurst(live(port))
        await tx.start_stream(1_000_000, 50_000, Broken(), True)
        assert tx.task is not None
        await asyncio.wait({tx.task}, timeout=5)
        await asyncio.sleep(0.05)
    assert tx.outcome == "failed: ValueError"
    assert not tx.active
    assert fake.control.endswith(PTT_LOW)
    # The board FIFO alone holds 123 ms at 50 kS/s.
    assert fake.closed_at[2] - fake.closed_at[3] > 0.1


# --- Mock path. ---


async def test_mock_waveform_uses_no_hardware_and_ends(monkeypatch):
    forbid_sockets(monkeypatch)
    service = Service(Settings())
    started = await service.tx_waveform(**{**GOOD, "acknowledge_rf_output": False})
    assert started["simulated"] and started["tx_active"] and started["outcome"] == "mock"
    assert started["rf_output_verified"] is False
    assert started["waveform"]["baseband_sha256"] == SHA_0_96_001
    assert started["waveform"]["sample_rate"] == 250_000
    assert started["waveform"]["symbol_rate_hz"] == 31_250
    assert started["waveform"]["burst_samples"] == 1408
    assert started["waveform"]["occupied_band_hz"] == [978_906.25, 1_021_093.75]
    requested = started["requested_settings"]
    assert requested["waveform"] == "aura_qpsk_v1" and requested["key_ptt"] is False
    assert "Drives the OUT connector" in started["side_effects"]
    await asyncio.sleep(0.05)
    assert not service.tx_status()["tx_active"]


async def test_mock_second_burst_is_refused_while_one_runs():
    service = Service(Settings())
    await service.tx_waveform(**{**GOOD, "repetitions": 20, "gap_s": 0.1})
    with pytest.raises(ValueError, match="already running"):
        await service.tx_waveform(**GOOD)
    with pytest.raises(ValueError, match="already running"):
        await service.tx_start(1_000_000, 50_000, 1_000.0, 0.05, 0.1, False, False)
    assert not (await service.tx_stop())["tx_active"]


async def test_mock_refusal_keeps_no_settings():
    service = Service(Settings(allow_lifecycle=True))
    await service.lifecycle("stop", False)
    with pytest.raises(ConnectionError):
        await service.tx_waveform(**GOOD)
    assert service.tx_status()["requested_settings"] is None


def test_profile_lists_the_built_in_waveforms():
    assert Service(Settings()).profile()["tx_waveforms"] == ["aura_qpsk_v1"]


# --- Live streaming against the loopback fake board. ---


async def test_waveform_is_streamed_exactly_with_silence_around_it():
    request = {**GOOD, "repetitions": 3, "gap_s": 0.01}
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        result = await service.tx_waveform(**request)
        assert result["tx_active"] and not result["simulated"]
        assert service.tx.task is not None
        await asyncio.wait_for(asyncio.shield(service.tx.task), 3)
        await asyncio.sleep(0.05)
    assert service.tx_status()["outcome"] == "completed"
    assert fake.roles == [2, 3]
    assert fake.control == tx_frequency(1_000_000) + tx_rate(250_000)
    silent = silent_blocks(250_000)
    stream = BurstStream(generate().samples, 250_000, 3, 2500)
    assert len(fake.data) == (silent + stream.blocks + TX_FLUSH_BLOCKS) * BLOCK
    assert not any(fake.data[: silent * BLOCK])
    assert not any(fake.data[-TX_FLUSH_BLOCKS * BLOCK :])
    sent = bytes(fake.data[silent * BLOCK : (silent + stream.blocks) * BLOCK])
    assert sent == b"".join(stream.block(index) for index in range(stream.blocks))
    # The first burst on the wire is the one whose SHA-256 was reported.
    first = sent[: 1408 * 8]
    assert hashlib.sha256(first).hexdigest() == result["waveform"]["baseband_sha256"]
    assert service.tx.blocks_sent == stream.blocks == result["waveform"]["blocks"]


async def test_ptt_is_off_unless_asked_and_drops_after_the_tail():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_waveform(**{**GOOD, "key_ptt": True})
        assert service.tx.task is not None
        await asyncio.wait_for(asyncio.shield(service.tx.task), 3)
        await asyncio.sleep(0.05)
    assert fake.control == tx_frequency(1_000_000) + tx_rate(250_000) + PTT_HIGH + PTT_LOW


async def test_early_stop_ends_on_a_falling_edge_and_flushes():
    fake = Board()
    request = {**GOOD, "sample_rate": 50_000, "repetitions": 100}
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_waveform(**request)
        await asyncio.sleep(0.2)
        await asyncio.wait_for(service.tx_stop(), 3)
        await asyncio.sleep(0.05)
    assert service.tx_status()["outcome"] == "stopped"
    stream = BurstStream(generate().samples, 50_000, 100, 0)
    sent = blocks_of(bytes(fake.data))[silent_blocks(50_000) : -TX_FLUSH_BLOCKS]
    assert 1 < len(sent) < stream.blocks
    assert sent[:-1] == [stream.block(index) for index in range(len(sent) - 1)]
    assert sent[-1] == stream.falling(len(sent) - 1)
    last = struct.unpack(f"<{2 * TX_BLOCK_SAMPLES}f", sent[-1])
    assert any(last) and last[-2:] == (0.0, 0.0)
    assert not any(fake.data[-TX_FLUSH_BLOCKS * BLOCK :])


async def test_tone_and_waveform_share_one_transmitter():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_waveform(**{**GOOD, "repetitions": 50})
        with pytest.raises(ValueError, match="already running"):
            await service.tx_start(1_000_000, 50_000, 1_000.0, 0.05, 0.1, False, True)
        await service.tx_stop()


# --- The registry is fixed. ---


def test_registry_cannot_be_changed_at_runtime():
    spec = WAVEFORMS["aura_qpsk_v1"]
    with pytest.raises(TypeError):
        WAVEFORMS["evil"] = spec  # type: ignore[index]
    with pytest.raises(TypeError):
        del WAVEFORMS["aura_qpsk_v1"]  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.generate = lambda *args: None  # type: ignore[misc]
    assert list(WAVEFORMS) == ["aura_qpsk_v1"]
    assert waveform_spec("aura_qpsk_v1") is spec


@pytest.mark.parametrize("amplitude", [0, -0.1, 1.01, math.nan, math.inf, True, "0.1"])
def test_generator_refuses_amplitudes_outside_full_scale(amplitude):
    with pytest.raises(ValueError, match="full scale"):
        generate(amplitude)


def test_generated_arrays_are_read_only_and_copies_do_not_leak_back():
    wave = generate()
    with pytest.raises(ValueError):
        wave.samples[0] = 1
    taps = rrc_taps()
    taps[:] = 0
    assert generate().details["baseband_sha256"] == SHA_0_96_001


async def test_tool_input_cannot_supply_samples_or_new_waveforms():
    server = build_server(Settings())
    tools = {tool.name: tool for tool in await server.list_tools()}
    schema = tools["rp_tx_waveform"].inputSchema
    assert set(schema["properties"]) == {
        "waveform",
        "frequency_hz",
        "amplitude",
        "sample_rate",
        "burst_index",
        "n_payload",
        "repetitions",
        "gap_s",
        "key_ptt",
        "acknowledge_rf_output",
    }
    assert schema["properties"]["waveform"].get("const") == "aura_qpsk_v1"
    assert all(
        prop.get("type") in {"integer", "number", "boolean", "string"}
        for prop in schema["properties"].values()
    )
    assert tools["rp_tx_waveform"].annotations.readOnlyHint is False
    # Unknown names fail validation before any service code runs.
    with pytest.raises(ToolError, match="aura_qpsk_v1"):
        await server.call_tool(
            "rp_tx_waveform", {"waveform": "evil", "frequency_hz": 1_000_000, "amplitude": 0.01}
        )
    # Extra keys are dropped: the result is the built-in burst, and nothing is registered.
    _, result = await server.call_tool(
        "rp_tx_waveform",
        {
            "waveform": "aura_qpsk_v1",
            "frequency_hz": 1_000_000,
            "amplitude": 0.01,
            "samples": [[1.0, 0.0]] * 64,
            "path": "/etc/passwd",
            "expression": "np.ones(4096)",
            "WAVEFORMS": {"evil": "x"},
        },
    )
    assert isinstance(result, dict)
    assert result["waveform"]["baseband_sha256"] == SHA_0_96_001
    assert list(WAVEFORMS) == ["aura_qpsk_v1"]
