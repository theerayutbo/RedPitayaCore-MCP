import asyncio
import contextlib
import math
import struct
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.protocol import (
    PTT_HIGH,
    PTT_LOW,
    ramp_blocks,
    tone_block,
    tx_frequency,
    tx_rate,
    tx_role,
)
from redpitayacore_mcp.service import Service
from redpitayacore_mcp.transport import (
    TX_FLUSH_BLOCKS,
    TX_PREROLL_BLOCKS,
    TX_START_SILENCE_S,
    TxBurst,
)

# Silent blocks before the tone at 50 kS/s: the gate's preroll, then the lead-in.
SILENT = TX_PREROLL_BLOCKS + math.ceil(TX_START_SILENCE_S * 50_000 / 2048)

BLOCK = 16384


def live(port: int = 1001, **changes) -> Settings:
    base = Settings(
        mode="live",
        host="127.0.0.1",
        port=port,
        timeout=1,
        allow_tx=True,
        tx_min_hz=900_000,
        tx_max_hz=1_100_000,
    )
    return replace(base, **changes)


class Board:
    """A stand-in for the server's TX roles that records what each role received."""

    def __init__(
        self,
        refuse: frozenset[int] = frozenset(),
        drop_data_after: int | None = None,
        drop_control_after: int | None = None,
    ):
        self.refuse = refuse
        self.drop_data_after = drop_data_after
        self.drop_control_after = drop_control_after
        self.roles: list[int] = []
        self.closed_at: dict[int, float] = {}
        self.events: list[object] = []
        self.control = bytearray()
        self.data = bytearray()

    async def handle(self, reader, writer):
        self.events.append("open")
        try:
            role = struct.unpack("<I", await reader.readexactly(4))[0]
        except asyncio.IncompleteReadError:
            self.events.append("idle socket closed")
            writer.close()
            return
        self.events.append(role)
        self.roles.append(role)
        if role in self.refuse:
            writer.close()
            return
        sink = self.control if role == 2 else self.data
        while chunk := await reader.read(65536):
            sink.extend(chunk)
            if role == 3 and self.drop_data_after and len(sink) >= self.drop_data_after:
                break
            if role == 2 and self.drop_control_after and len(sink) >= self.drop_control_after:
                await asyncio.sleep(0.3)
                break
        self.closed_at[role] = asyncio.get_running_loop().time()
        writer.close()


@asynccontextmanager
async def board(fake: Board):
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


def test_words_match_the_server():
    assert tx_role("control") == struct.pack("<I", 2)
    assert tx_role("data") == struct.pack("<I", 3)
    assert PTT_HIGH == struct.pack("<I", 2 << 28)
    assert PTT_LOW == struct.pack("<I", 3 << 28)
    assert tx_frequency(7_100_000) == struct.pack("<I", 7_100_000)
    assert tx_rate(50_000) == struct.pack("<I", 1 << 28 | 1)


@pytest.mark.parametrize("role", [0, 1, "rx", None])
def test_tx_role_has_only_two_values(role):
    with pytest.raises(ValueError):
        tx_role(role)


@pytest.mark.parametrize("value", [0, 60_000_001, True, 1.5])
def test_bad_tx_frequency(value):
    with pytest.raises(ValueError):
        tx_frequency(value)


def test_tone_block_is_periodic_and_never_exceeds_amplitude():
    raw, actual = tone_block(50_000, 5_000, 0.1)
    assert len(raw) == BLOCK
    assert actual == pytest.approx(5_004.8828125)
    values = struct.unpack(f"<{len(raw) // 4}f", raw)
    pairs = list(zip(values[::2], values[1::2], strict=True))
    assert max(math.hypot(*pair) for pair in pairs) <= 0.1 * (1 + 1e-6)
    step = 2 * math.pi * actual / 50_000
    assert pairs[0] == (pytest.approx(0.1), 0)
    next_i = 0.1 * math.cos(step * 2048)
    assert next_i == pytest.approx(pairs[0][0], abs=1e-6)


def test_negative_offset_is_conjugate():
    positive = struct.unpack("<4096f", tone_block(50_000, 5_000, 0.1)[0])
    negative = struct.unpack("<4096f", tone_block(50_000, -5_000, 0.1)[0])
    assert negative[0::2] == positive[0::2]
    assert negative[1::2] == pytest.approx([-value for value in positive[1::2]])


@pytest.mark.parametrize(
    "env",
    [
        {"RP_ALLOW_TX": "true"},
        {"RP_ALLOW_TX": "true", "RP_TX_MIN_HZ": "1000"},
        {"RP_TX_MAX_HZ": "1000"},
        {"RP_TX_MIN_HZ": "2000", "RP_TX_MAX_HZ": "1000"},
        {"RP_TX_MIN_HZ": "0", "RP_TX_MAX_HZ": "1000"},
        {"RP_TX_MIN_HZ": "1", "RP_TX_MAX_HZ": "60000001"},
        {"RP_TX_MAX_AMPLITUDE": "0"},
        {"RP_TX_MAX_AMPLITUDE": "1.01"},
        {"RP_TX_MAX_AMPLITUDE": "nan"},
        {"RP_TX_MAX_SECONDS": "0"},
        {"RP_TX_MAX_SECONDS": "180.01"},
        {"RP_TX_MAX_SECONDS": "inf"},
        {"RP_TX_LEAD_S": "0.01"},
        {"RP_TX_LEAD_S": "2"},
        {"RP_TX_LEAD_S": "nan"},
    ],
)
def test_tx_config_fails_closed(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


def test_tx_defaults_are_off():
    config = Settings.from_env({})
    assert not config.allow_tx
    assert config.tx_min_hz is None
    assert config.tx_max_amplitude == 0.1


GOOD = {
    "frequency_hz": 1_000_000,
    "sample_rate": 50_000,
    "tone_offset_hz": 5_000.0,
    "amplitude": 0.05,
    "duration_s": 0.1,
    "key_ptt": False,
    "acknowledge_rf_output": True,
}


def forbid_sockets(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A refused call must not open a socket")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)


async def test_live_tx_needs_the_operator_switch(monkeypatch):
    forbid_sockets(monkeypatch)
    with pytest.raises(PermissionError, match="RP_ALLOW_TX"):
        await Service(live(allow_tx=False, tx_min_hz=None, tx_max_hz=None)).tx_start(**GOOD)


async def test_live_tx_needs_a_per_call_acknowledgement(monkeypatch):
    forbid_sockets(monkeypatch)
    with pytest.raises(PermissionError, match="acknowledge_rf_output"):
        await Service(live()).tx_start(**{**GOOD, "acknowledge_rf_output": False})


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"frequency_hz": 2_000_000}, PermissionError),
        # The carrier is in band but the tone is not.
        ({"frequency_hz": 1_099_000, "tone_offset_hz": 2_000.0}, PermissionError),
        ({"amplitude": 0.11}, ValueError),
        ({"amplitude": 0}, ValueError),
        ({"amplitude": -0.05}, ValueError),
        ({"amplitude": math.nan}, ValueError),
        ({"duration_s": 10.1}, ValueError),
        ({"duration_s": 0}, ValueError),
        # Shorter than one 41 ms block at 50 kS/s.
        ({"duration_s": 0.04}, ValueError),
        ({"duration_s": math.inf}, ValueError),
        ({"tone_offset_hz": 20_001.0}, ValueError),
        ({"tone_offset_hz": math.nan}, ValueError),
        ({"sample_rate": 48_000}, ValueError),
    ],
)
async def test_limits_refuse_before_any_socket(monkeypatch, change, error):
    forbid_sockets(monkeypatch)
    with pytest.raises(error):
        await Service(live()).tx_start(**{**GOOD, **change})


async def test_mock_tx_uses_no_hardware_and_ends(monkeypatch):
    forbid_sockets(monkeypatch)
    service = Service(Settings())
    started = await service.tx_start(**{**GOOD, "acknowledge_rf_output": False})
    assert started["simulated"] and started["tx_active"]
    await asyncio.sleep(0.15)
    assert not service.tx_status()["tx_active"]


async def test_burst_ends_by_itself_with_whole_blocks_and_zero_tail():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        result = await service.tx_start(**GOOD)
        assert result["tx_active"]
        assert result["requested_settings"]["tone_offset_hz"] != 5_000.0
        assert service.tx.task is not None
        await asyncio.wait_for(asyncio.shield(service.tx.task), 3)
        await asyncio.sleep(0.05)
    assert service.tx_status()["outcome"] == "completed"
    assert fake.roles == [2, 3]
    assert fake.control == tx_frequency(1_000_000) + tx_rate(50_000)
    assert len(fake.data) % BLOCK == 0
    tail = fake.data[-TX_FLUSH_BLOCKS * BLOCK :]
    assert not any(tail)
    # 0.1 s at 50 kS/s is two whole blocks, rounded down so the limit holds.
    assert service.tx.blocks_sent == 2
    assert result["requested_settings"]["duration_s"] == pytest.approx(0.08192)
    assert len(fake.data) == (SILENT + 2 + TX_FLUSH_BLOCKS) * BLOCK
    assert not any(fake.data[: SILENT * BLOCK])
    assert any(fake.data[SILENT * BLOCK : (SILENT + 1) * BLOCK])


async def test_first_tx_blocks_are_queued_behind_an_idle_socket():
    """The stock server's first recv must find a whole block, or stack reaches the DAC."""
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**GOOD)
        await service.tx_stop()
        await asyncio.sleep(0.05)
    assert fake.events == [
        "open",
        2,
        "open",  # the idle socket
        "open",
        3,
        "idle socket closed",
    ]


def magnitudes(raw: bytes) -> list[float]:
    values = struct.unpack(f"<{len(raw) // 4}f", raw)
    return [math.hypot(i, q) for i, q in zip(values[::2], values[1::2], strict=True)]


@pytest.mark.parametrize("rate", [20_000, 50_000, 1_250_000])
def test_ramps_start_and_end_at_zero_and_never_exceed_the_tone(rate):
    block = tone_block(rate, 5_000, 0.1)[0]
    rising, falling, both = (magnitudes(raw) for raw in ramp_blocks(block, rate))
    assert rising[0] == 0 and falling[-1] == 0 and both[0] == 0 and both[-1] == 0
    assert max(rising + falling + both) <= 0.1 * (1 + 1e-6)
    assert rising[-1] == pytest.approx(0.1) and falling[0] == pytest.approx(0.1)
    # The rise is monotonic, so it adds no ripple of its own.
    length = min(2048, round(0.005 * rate))
    assert all(a <= b + 1e-9 for a, b in zip(rising[: length - 1], rising[1:length], strict=True))


def blocks_of(data: bytes) -> list[bytes]:
    return [data[index : index + BLOCK] for index in range(0, len(data), BLOCK)]


async def test_burst_has_both_edges_and_full_blocks_between():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 0.2})
        assert service.tx.task is not None
        await asyncio.wait({service.tx.task}, timeout=3)
    sent = blocks_of(bytes(fake.data))[SILENT:-TX_FLUSH_BLOCKS]
    rising, falling, _ = ramp_blocks(tone_block(50_000, 5_000, 0.05)[0], 50_000)
    full = tone_block(50_000, 5_000, 0.05)[0]
    assert sent == [rising, full, full, falling]


async def test_single_block_burst_ramps_both_ways():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 0.05})
        assert service.tx.task is not None
        await asyncio.wait({service.tx.task}, timeout=3)
    sent = blocks_of(bytes(fake.data))[SILENT:-TX_FLUSH_BLOCKS]
    assert sent == [ramp_blocks(tone_block(50_000, 5_000, 0.05)[0], 50_000)[2]]


async def test_early_stop_ends_on_a_falling_edge():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 5.0})
        await asyncio.sleep(0.2)
        await service.tx_stop()
    sent = blocks_of(bytes(fake.data))[SILENT:-TX_FLUSH_BLOCKS]
    falling = ramp_blocks(tone_block(50_000, 5_000, 0.05)[0], 50_000)[1]
    assert sent[-1] == falling
    assert len(sent) <= service.tx_status()["requested_settings"]["duration_s"] * 50_000 / 2048


async def test_lead_setting_controls_how_far_ahead_samples_go():
    sent_ahead = {}
    for lead in (0.1, 0.5):
        fake = Board()
        async with board(fake) as port:
            service = Service(live(port, tx_lead_s=lead))
            await service.tx_start(**{**GOOD, "duration_s": 5.0})
            await asyncio.sleep(0.3)
            sent_ahead[lead] = service.tx.blocks_sent
            await service.tx_stop()
    assert sent_ahead[0.5] > sent_ahead[0.1] + 5


async def test_ptt_is_off_unless_asked_and_drops_after_the_tail():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "key_ptt": True})
        assert service.tx.task is not None
        await asyncio.wait_for(asyncio.shield(service.tx.task), 3)
        await asyncio.sleep(0.05)
    assert fake.control == tx_frequency(1_000_000) + tx_rate(50_000) + PTT_HIGH + PTT_LOW


async def test_stop_ends_early_and_still_flushes():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 5.0})
        await asyncio.sleep(0.1)
        await asyncio.wait_for(service.tx_stop(), 3)
        await asyncio.sleep(0.05)
    assert service.tx_status()["outcome"] == "stopped"
    assert service.tx.blocks_sent < 20
    assert not any(fake.data[-TX_FLUSH_BLOCKS * BLOCK :])


async def test_second_burst_is_refused_while_one_runs():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 2.0})
        with pytest.raises(ValueError, match="already running"):
            await service.tx_start(**GOOD)
        await service.tx_stop()


@pytest.mark.parametrize("role", [2, 3])
async def test_refused_role_sends_no_samples(role):
    fake = Board(refuse=frozenset({role}))
    async with board(fake) as port:
        service = Service(live(port))
        with pytest.raises(ConnectionError, match="role ownership"):
            await service.tx_start(**{**GOOD, "key_ptt": True})
        await asyncio.sleep(0.05)
    assert not service.tx.active
    assert not any(fake.data)
    assert PTT_HIGH not in fake.control


async def test_server_drop_is_reported_as_failure():
    fake = Board(drop_data_after=6 * BLOCK)
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 2.0})
        assert service.tx.task is not None
        await asyncio.wait_for(asyncio.shield(service.tx.task), 5)
    assert service.tx_status()["outcome"].startswith("failed")
    assert not service.tx.active


async def test_shutdown_ends_the_burst_in_order():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 5.0, "key_ptt": True})
        await asyncio.sleep(0.05)
        await asyncio.wait_for(service.shutdown(), 3)
        await asyncio.sleep(0.05)
    assert not service.tx.active
    assert service.tx_status()["outcome"] == "stopped"
    assert fake.control.endswith(PTT_LOW)


async def test_control_stays_open_until_the_queue_has_played():
    """Closing TX control retunes the board, so no failure may close it with tone queued."""
    fake = Board(drop_data_after=6 * BLOCK)
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    service = Service(live(port))
    began = asyncio.get_running_loop().time()
    await service.tx_start(**{**GOOD, "duration_s": 2.0, "key_ptt": True})
    assert service.tx.task is not None
    await asyncio.wait({service.tx.task}, timeout=5)
    ended = asyncio.get_running_loop().time()
    server.close()
    assert service.tx_status()["outcome"].startswith("failed")
    # The lead and the board FIFO are both still playing when the data socket drops.
    assert ended - began > 0.3
    assert fake.control.endswith(PTT_LOW)


async def test_a_stall_extends_the_ptt_tail():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 0.5, "key_ptt": True})
        await asyncio.sleep(0.1)
        began = asyncio.get_running_loop().time()
        time.sleep(0.6)  # noqa: ASYNC251 - the stall under test
        assert service.tx.task is not None
        await asyncio.wait({service.tx.task}, timeout=3)
        ended = asyncio.get_running_loop().time()
    # Blocks sent after the stall still need their playing time.
    assert ended - began > 0.6 + 0.3


async def test_cancelled_stop_leaves_the_burst_ending_cleanly():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 5.0, "key_ptt": True})
        await asyncio.sleep(0.05)
        waiter = asyncio.create_task(service.tx_stop())
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert service.tx.task is not None
        await asyncio.wait({service.tx.task}, timeout=3)
        await asyncio.sleep(0.05)
        result = await service.tx_stop()
    assert result["outcome"] == "stopped"
    assert fake.control.endswith(PTT_LOW)


async def test_cancelled_task_holds_control_until_the_queue_has_played():
    """Ctrl-C cancels the feed. Closing TX control at once would move queued tone to 600 kHz."""
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 5.0})
        await asyncio.sleep(0.3)
        assert service.tx.task is not None
        cancelled = asyncio.get_running_loop().time()
        service.tx.task.cancel()
        await asyncio.wait({service.tx.task}, timeout=3)
        await asyncio.sleep(0.05)
        # Data goes at once; control waits for the lead plus the FIFO, over 0.2 s at 50 kS/s.
        assert fake.closed_at[3] - cancelled < 0.1
        assert fake.closed_at[2] - cancelled > 0.2
        assert (await service.tx_stop())["outcome"] == "aborted"
        await asyncio.sleep(0.25)
        await service.tx_start(**GOOD)
        await service.tx_stop()


async def test_cancelled_start_never_leaves_the_data_role_held():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        for delay in (0.06, 0.08, 0.1, 0.12, 0.14, 0.16):
            starting = asyncio.create_task(service.tx_start(**GOOD))
            await asyncio.sleep(delay)
            starting.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                await starting
            await service.tx_stop()
            await asyncio.sleep(0.25)
        opened = sum(1 for role in fake.roles if role == 3)
        closed = 3 in fake.closed_at
        assert opened == 0 or closed
        # The role is free again, so a new burst starts and ends normally.
        await service.tx_start(**GOOD)
        await service.tx_stop()
    assert service.tx_status()["outcome"] == "stopped"


async def test_mock_refusal_keeps_no_settings():
    service = Service(Settings(allow_lifecycle=True))
    await service.lifecycle("stop", False)
    with pytest.raises(ConnectionError):
        await service.tx_start(**GOOD)
    assert service.tx_status()["requested_settings"] is None


async def test_failed_start_is_reported_as_failed():
    fake = Board()
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**GOOD)
        await service.tx_stop()
    with pytest.raises(ConnectionError):
        await service.tx_start(**GOOD)
    status = service.tx_status()
    assert status["outcome"] == "start_failed"
    assert status["blocks_sent"] == 0
    assert not status["tx_active"]


async def test_lost_control_stops_the_feed():
    fake = Board(drop_control_after=8)
    async with board(fake) as port:
        service = Service(live(port))
        await service.tx_start(**{**GOOD, "duration_s": 5.0})
        assert service.tx.task is not None
        await asyncio.wait({service.tx.task}, timeout=3)
    assert service.tx_status()["outcome"].startswith("failed")
    assert service.tx.blocks_sent < 30


async def test_mock_applies_the_band_when_one_is_set():
    service = Service(Settings(tx_min_hz=900_000, tx_max_hz=1_100_000))
    with pytest.raises(PermissionError):
        await service.tx_start(**{**GOOD, "frequency_hz": 7_100_000})


async def test_mock_stop_app_ends_a_mock_burst():
    service = Service(Settings(allow_lifecycle=True))
    await service.tx_start(**{**GOOD, "duration_s": 5.0})
    await service.lifecycle("stop", False)
    assert not service.tx_status()["tx_active"]
    with pytest.raises(ConnectionError):
        await service.tx_start(**GOOD)


async def test_transport_also_refuses_without_the_switch(monkeypatch):
    forbid_sockets(monkeypatch)
    with pytest.raises(PermissionError):
        await TxBurst(live(allow_tx=False, tx_min_hz=None, tx_max_hz=None)).start(
            1_000_000, 50_000, bytes(BLOCK), 2, False
        )


@pytest.mark.parametrize("seconds", ["0.1", "60", "132", "180"])
def test_tx_max_seconds_accepts_up_to_180(seconds):
    settings = Settings.from_env({"RP_TX_MAX_SECONDS": seconds})
    assert settings.tx_max_seconds == float(seconds)
