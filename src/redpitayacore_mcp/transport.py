"""Bounded, fixed-target I/O. No generic remote command or HTTP mutation API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import socket
import tempfile
from typing import Any

import httpx
import numpy as np

from .config import Settings, ssh_password_for
from .protocol import (
    MAX_SAMPLES,
    PTT_HIGH,
    PTT_LOW,
    TX_BLOCK_SAMPLES,
    decode_iq_array,
    ramp_blocks,
    rx_frequency,
    rx_rate,
    rx_role,
    tx_frequency,
    tx_rate,
    tx_role,
)
from .rx_order import BLOCK_BYTES, restore_block

# Any GET under /bazaar reaches the board's app controller. Measured on a STEMlab 125-14,
# /bazaar?help and /bazaar?apps stop a running sdr_transceiver within 0.5 s. Never add one here.
HTTP_PATHS = frozenset({"/sdr_transceiver/info/info.json"})
MAX_RESPONSE = 65536
# Measured on a STEMlab 125-14: reconnecting 0 ms after close was dropped, 50 ms worked.
RX_REOPEN_GUARD_S = 0.2
# Tuning settles before the data role resets the FIFO.
RX_SETTLE_S = 0.05
# The server's short-read hazard needs data waiting in its socket, so stay at least four blocks
# ahead. RP_TX_LEAD_S sets the lead in seconds, which must exceed the host's worst stall.
TX_MIN_LEAD_BLOCKS = 4
# Silence queued before the server starts reading TX data. See TxBurst._open_data.
TX_PREROLL_BLOCKS = 2
TX_FLUSH_BLOCKS = 3
# Silence sent ahead of the rising edge, so the board's buffer fills before the tone starts. At
# 1.25 MS/s, none left 2 of 5 starts chopped (-28 dBc beside the tone); 20 or 50 ms left none.
TX_START_SILENCE_S = 0.05
# The board FIFO holds up to this many samples behind the socket before the DAC.
TX_FIFO_SAMPLES = 6144
# Board health via read-only sysfs/procfs: Zynq XADC die temperature and supply rails (IIO device
# named "xadc"; its index differs between OS images), uptime, load average and eth0 error counters.
# Prints key=value lines only. Never reads MAC addresses or the Zynq DNA.
BOARD_HEALTH_RAILS = ("vccint", "vccaux", "vccbram", "vccpint", "vccpaux", "vccoddr")
BOARD_HEALTH_COMMAND = (
    'd=""; for x in /sys/bus/iio/devices/iio:device*; do '
    'if [ -r "$x/name" ] && [ "$(cat "$x/name")" = xadc ]; then d="$x"; fi; done; '
    'if [ -n "$d" ]; then '
    'echo "temp_raw=$(cat "$d/in_temp0_raw")"; echo "temp_offset=$(cat "$d/in_temp0_offset")"; '
    'echo "temp_scale=$(cat "$d/in_temp0_scale")"; '
    "for c in " + " ".join(BOARD_HEALTH_RAILS) + "; do "
    'for f in "$d"/in_voltage*_"$c"_raw; do [ -r "$f" ] && echo "${c}_raw=$(cat "$f")"; done; '
    'for f in "$d"/in_voltage*_"$c"_scale; do [ -r "$f" ] && echo "${c}_scale=$(cat "$f")"; done; '
    "done; fi; "
    'echo "uptime_s=$(cut -d" " -f1 /proc/uptime)"; '
    'echo "loadavg=$(cut -d" " -f1-3 /proc/loadavg)"; '
    "for k in rx_dropped rx_errors tx_dropped tx_errors; do f=/sys/class/net/eth0/statistics/$k; "
    '[ -r "$f" ] && echo "eth0_${k}=$(cat "$f")"; done; true'
)

SSH_COMMANDS = {
    "os_release": "cat /etc/os-release",
    "board_health": BOARD_HEALTH_COMMAND,
    # sysinfo.json also holds the MAC and DNA, so read only the name line. OS 3.00 has no
    # devicetree model node.
    "board_model": "grep '^\"name\"' /tmp/sysinfo.json",
    "ecosystem_version": "cat /opt/redpitaya/version.txt",
    "app_info": "cat /opt/redpitaya/www/apps/sdr_transceiver/info/info.json",
    "process_names": "ps -eo comm=",
    "fpga_state": "cat /sys/class/fpga_manager/fpga0/state",
    # The SDR app's fpga.sh deletes this file, so its absence is normal while the app is loaded.
    "loaded_image_label": "if [ -e /tmp/loaded_fpga.inf ]; then cat /tmp/loaded_fpga.inf; "
    "else echo absent; fi",
    "fpga_flags": "cat /sys/class/fpga_manager/fpga0/flags",
    "overlay_inventory": "ls -1 /configfs/device-tree/overlays",
    "installed_bitstream_sha256": "sha256sum /opt/redpitaya/www/apps/sdr_transceiver/fpga.bit.bin",
    "installed_dtbo_sha256": "sha256sum /opt/redpitaya/www/apps/sdr_transceiver/fpga.dtbo",
    "installed_server_sha256": "sha256sum /opt/redpitaya/www/apps/sdr_transceiver/sdr-transceiver",
}


def _require_live(config: Settings) -> str:
    if config.mode != "live" or config.host is None:
        raise ValueError("Hardware transport requires explicit live mode and RP_HOST")
    return config.host


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is not None:
        writer.close()
        await _wait_writer_closed(writer)


async def _wait_writer_closed(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 1)


def _probe_failure(error: OSError | TimeoutError) -> str:
    if isinstance(error, ConnectionRefusedError):
        return "refused"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "network_error"


async def probe_tcp(config: Settings) -> dict[str, Any]:
    host = _require_live(config)
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, config.port, family=socket.AF_INET), config.timeout
        )
        return {
            "reachable": True,
            "selector_sent": False,
            "protocol_verified": False,
            "note": "TCP accept only; no app/build identification",
        }
    except (OSError, TimeoutError) as error:
        return {
            "reachable": False,
            "failure": _probe_failure(error),
            "selector_sent": False,
            "protocol_verified": False,
            "note": "Check board address, network and operator-started SDR app",
        }
    finally:
        await _close_writer(writer)


class DeviceHTTP:
    def __init__(self, config: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    async def get_json(self, path: str) -> Any:
        if path not in HTTP_PATHS:
            raise ValueError("HTTP path is outside the read-only allowlist")
        host = _require_live(self.config)
        async with (
            httpx.AsyncClient(
                timeout=self.config.timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"Accept-Encoding": "identity"},
                transport=self.transport,
            ) as client,
            asyncio.timeout(self.config.timeout),
        ):
            async with client.stream(
                "GET", f"http://{host}:{self.config.http_port}{path}"
            ) as response:
                response.raise_for_status()
                if (
                    response.headers.get("Content-Encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise ValueError("Compressed diagnostic responses are not accepted")
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=4096):
                    if len(body) + len(chunk) > MAX_RESPONSE:
                        raise ValueError("HTTP response exceeds 65536 bytes")
                    body.extend(chunk)
                return json.loads(body)


def _make_askpass_helper() -> tuple[str, str]:
    """Private 0700 dir with a helper printing $RP_ASKPASS_SECRET (no secret on disk)."""
    askpass_dir = tempfile.mkdtemp(prefix="rp-askpass-")
    helper = os.path.join(askpass_dir, "askpass")
    with open(helper, "w", encoding="ascii") as handle:
        handle.write("#!/bin/sh\nprintf '%s\\n' \"$RP_ASKPASS_SECRET\"\n")
    os.chmod(helper, 0o700)
    return askpass_dir, helper


async def _ssh_command(config: Settings, command: str) -> dict[str, Any]:
    host = _require_live(config)
    if command not in SSH_COMMANDS.values() or not config.ssh_user:
        raise ValueError("SSH requires an allowlisted command and RP_SSH_USER")
    # Fixed argv, no local shell. Remote command is a literal from SSH_COMMANDS.
    password = ssh_password_for(config)
    if password is None:
        auth = [
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
        ]
        env = None
        askpass_dir = None
    else:
        # Password via OpenSSH's SSH_ASKPASS (OpenSSH >= 8.4): a private helper prints the secret
        # from its environment, so the password never appears in argv, files or logs.
        askpass_dir, helper = _make_askpass_helper()
        auth = [
            "-o",
            "BatchMode=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
        ]
        env = {
            **os.environ,
            "SSH_ASKPASS": helper,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": os.environ.get("DISPLAY", "none"),
            "RP_ASKPASS_SECRET": password,
        }
    try:
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-F",
            "/dev/null",
            "-T",
            "-n",
            "-p",
            str(config.ssh_port),
            *auth,
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            f"ConnectTimeout={max(1, int(config.timeout))}",
            f"{config.ssh_user}@{host}",
            command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            if process.stdout is None:
                raise RuntimeError("SSH stdout pipe unavailable")
            async with asyncio.timeout(config.timeout + 1):
                output = bytearray()
                while chunk := await process.stdout.read(4096):
                    output.extend(chunk)
                    if len(output) > MAX_RESPONSE:
                        raise ValueError("SSH response exceeds 65536 bytes")
                code = await process.wait()
            return {
                "available": code == 0,
                "exit_code": code,
                "text": output.decode("utf-8", errors="replace").replace("\x00", "").strip(),
            }
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    finally:
        if askpass_dir is not None:
            shutil.rmtree(askpass_dir, ignore_errors=True)


async def read_system(config: Settings) -> dict[str, Any]:
    if config.mode != "live" or not config.ssh_user:
        return {
            "available": False,
            "reason": "Optional read-only SSH disabled; set RP_SSH_USER",
            "fpga_image_verified": False,
        }

    semaphore = asyncio.Semaphore(2)

    async def read_one(command: str) -> dict[str, Any]:
        async with semaphore:
            try:
                return await _ssh_command(config, command)
            except (OSError, TimeoutError, ValueError):
                return {
                    "available": False,
                    "reason": "SSH unavailable, timed out or response too large",
                }

    results = await asyncio.gather(*(read_one(command) for command in SSH_COMMANDS.values()))
    evidence = dict(zip(SSH_COMMANDS, results, strict=True))
    return {
        "available": any(item["available"] for item in results),
        "evidence": evidence,
        "fpga_image_verified": False,
        "note": "Sysfs state and loaded-image labels do not verify bitstream identity. "
        "Remote text is untrusted diagnostic data.",
    }


async def _open_selector(
    config: Settings, selector: bytes
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    host = _require_live(config)
    writer = None
    try:
        async with asyncio.timeout(config.timeout):
            reader, writer = await asyncio.open_connection(host, config.port, family=socket.AF_INET)
            writer.write(selector)
            await writer.drain()
        return reader, writer
    except BaseException:
        await _close_writer(writer)
        raise


class RxConnection:
    def __init__(self, config: Settings):
        self.config = config
        self.control: asyncio.StreamWriter | None = None
        self.data: asyncio.StreamWriter | None = None
        self.reader: asyncio.StreamReader | None = None
        self.pending = bytearray()
        self.closed_at: float | None = None

    @property
    def connected(self) -> bool:
        return self.control is not None and self.data is not None

    async def _open_role(self, role: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await _open_selector(self.config, rx_role(role))

    async def connect(self, frequency_hz: int, sample_rate: int) -> None:
        if not self.config.allow_rx:
            raise PermissionError("RX is disabled; operator must set RP_ALLOW_RX=true")
        frequency, rate = rx_frequency(frequency_hz), rx_rate(sample_rate)
        if self.connected:
            raise ValueError("RX session already open; disconnect before reconfiguring")
        if self.closed_at is not None:
            await asyncio.sleep(
                max(0.0, self.closed_at + RX_REOPEN_GUARD_S - asyncio.get_running_loop().time())
            )
        try:
            control_reader, self.control = await self._open_role("control")
            self.control.write(frequency + rate)
            await asyncio.wait_for(self.control.drain(), self.config.timeout)
            await asyncio.sleep(RX_SETTLE_S)
            self.reader, self.data = await self._open_role("data")
            await asyncio.sleep(RX_SETTLE_S)
            # The server sends no ACK. It closes a socket whose role has an owner, and a control
            # socket gets no data, so a closed socket is the only refusal a client can see. Unread
            # commands can turn that close into a reset, which sets an exception and not EOF.
            if any(
                reader.at_eof() or reader.exception() is not None
                for reader in (control_reader, self.reader)
            ):
                raise ConnectionError("RX role refused; another client owns the app")
        except BaseException:
            await self.close()
            raise

    async def read(self, count: int) -> list[tuple[float, float]]:
        pairs = (await self.read_array(count)).view(np.float32).reshape(-1, 2).tolist()
        return [(i, q) for i, q in pairs]

    async def read_array(self, count: int) -> np.ndarray:
        if type(count) is not int or not 1 <= count <= MAX_SAMPLES:
            raise ValueError("sample_count must be an integer from 1 to 8192")
        if not self.connected or self.reader is None:
            raise ConnectionError("No RX session; call rp_rx_connect first")
        try:
            async with asyncio.timeout(self.config.timeout):
                raw = await self._read_bytes(self.reader, count * 8)
            return decode_iq_array(raw)
        except asyncio.IncompleteReadError as error:
            await self.close()
            raise ConnectionError(
                "RX ended early; app/role may be unavailable or occupied"
            ) from error
        except BaseException:
            await self.close()
            raise

    async def _read_bytes(self, reader: asyncio.StreamReader, size: int) -> bytes:
        if self.config.rx_sample_order == "raw":
            return await reader.readexactly(size)
        # Order is only defined per whole block counted from the first byte of the connection.
        while len(self.pending) < size:
            self.pending += restore_block(await reader.readexactly(BLOCK_BYTES))
        data = bytes(self.pending[:size])
        del self.pending[:size]
        return data

    async def close(self) -> None:
        writers = [writer for writer in (self.data, self.control) if writer is not None]
        # Signal BOTH transports before any await: cancellation cannot orphan the second one.
        for writer in writers:
            writer.close()
        self.data, self.control, self.reader = None, None, None
        self.pending.clear()
        if writers:
            self.closed_at = asyncio.get_running_loop().time()
        await asyncio.gather(*(_wait_writer_closed(writer) for writer in writers))


class TxBurst:
    """One bounded, self-ending burst. Nothing here can transmit without an end time.

    Closing TX control makes the server drop PTT and retune to 600 kHz at 100 kS/s at once, while
    samples already queued keep playing. So TX control stays open until the queue has played out,
    on every path this process survives. If the process dies, the queued tail goes out near
    600 kHz: measured 0.12 to 0.30 s, and no client can prevent it. A client that stalls with
    control open leaves PTT high, and OUT falls silent when the samples run out.
    """

    def __init__(self, config: Settings):
        self.config = config
        self.task: asyncio.Task[None] | None = None
        self.stop_requested = asyncio.Event()
        self.starting = False
        self.blocks_sent = 0
        self.outcome = "never_started"
        self.ptt_high = False
        self.closed_at: float | None = None

    @property
    def active(self) -> bool:
        return self.starting or (self.task is not None and not self.task.done())

    async def start(
        self, frequency_hz: int, sample_rate: int, block: bytes, blocks: int, key_ptt: bool
    ) -> None:
        if not self.config.allow_tx:
            raise PermissionError("TX is disabled; operator must set RP_ALLOW_TX=true")
        words = tx_frequency(frequency_hz) + tx_rate(sample_rate)
        if len(block) != TX_BLOCK_SAMPLES * 8:
            raise ValueError("TX blocks must hold exactly 2048 samples")
        if type(blocks) is not int or blocks < 1:
            raise ValueError("A burst needs at least one whole block")
        if self.active:
            raise ValueError("TX burst already running; stop it first")
        self.starting = True
        self.blocks_sent = 0
        self.outcome = "starting"
        control = data = None
        try:
            if self.closed_at is not None:
                await asyncio.sleep(
                    max(0.0, self.closed_at + RX_REOPEN_GUARD_S - asyncio.get_running_loop().time())
                )
            control_reader, control = await _open_selector(self.config, tx_role("control"))
            control.write(words)
            await asyncio.wait_for(control.drain(), self.config.timeout)
            await asyncio.sleep(RX_SETTLE_S)
            data_reader, data = await self._open_data()
            await asyncio.sleep(RX_SETTLE_S)
            if any(
                reader.at_eof() or reader.exception() is not None
                for reader in (control_reader, data_reader)
            ):
                raise ConnectionError("TX role refused; another client owns the app")
            if key_ptt:
                self.ptt_high = True
                control.write(PTT_HIGH)
                await asyncio.wait_for(control.drain(), self.config.timeout)
                await asyncio.sleep(RX_SETTLE_S)
        except BaseException:
            # No tone is queued yet, and closing TX control makes the server drop PTT.
            self.ptt_high = False
            self.starting = False
            self.outcome = "start_failed"
            writers = [writer for writer in (data, control) if writer is not None]
            for writer in writers:
                writer.close()
            self.closed_at = asyncio.get_running_loop().time()
            await asyncio.gather(*(_wait_writer_closed(writer) for writer in writers))
            raise
        self.stop_requested = asyncio.Event()
        self.outcome = "running"
        self.task = asyncio.create_task(
            self._feed(control, control_reader, data, block, sample_rate, blocks),
            name="rp-tx-burst",
        )
        self.starting = False

    async def _open_data(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open TX data with its first block already waiting at the board.

        The server calls recv once and copies 16384 bytes. A first read that comes back short
        sends uninitialised stack to the DAC, measured as a pulse about 20 times a 0.1 full-scale
        tone. Its accept loop is one thread that blocks reading a new socket's role word, so an
        idle socket holds it while the kernel queues the real connection and its first blocks.
        Closing the idle socket then releases it, and the first recv finds a whole block.
        """
        host = _require_live(self.config)
        async with asyncio.timeout(self.config.timeout):
            _, gate = await asyncio.open_connection(host, self.config.port, family=socket.AF_INET)
        writer = None
        try:
            await asyncio.sleep(RX_SETTLE_S)
            silence = bytes(TX_BLOCK_SAMPLES * 8) * TX_PREROLL_BLOCKS
            reader, writer = await _open_selector(self.config, tx_role("data") + silence)
            await asyncio.sleep(RX_SETTLE_S)
        except BaseException:
            # Only silence is queued, so the data socket can go at once. Leaving it open would
            # hold the role until this process exits.
            if writer is not None:
                writer.close()
            raise
        finally:
            # No await after the data socket exists, so a cancellation cannot orphan it.
            gate.close()
        return reader, writer

    async def _feed(
        self,
        control: asyncio.StreamWriter,
        control_reader: asyncio.StreamReader,
        data: asyncio.StreamWriter,
        block: bytes,
        sample_rate: int,
        blocks: int,
    ) -> None:
        loop = asyncio.get_running_loop()
        block_s = TX_BLOCK_SAMPLES / sample_rate
        fifo_s = TX_FIFO_SAMPLES / sample_rate
        # Up to the FIFO's worth of what has been sent sits on the board, not in the socket, and
        # the server needs a whole block in the socket for each read. So RP_TX_LEAD_S is the
        # stall the socket can ride out, on top of those.
        lead_s = max(TX_MIN_LEAD_BLOCKS * block_s, self.config.tx_lead_s + fifo_s + block_s)
        rising, falling, single = ramp_blocks(block, sample_rate)
        silence = bytes(len(block))
        # When the DAC runs out of what has been sent. A stall moves this later, so pacing and the
        # PTT tail both follow what the DAC can really have played.
        dac_end = loop.time() + TX_PREROLL_BLOCKS * block_s
        outcome = "failed"

        def queue(payload: bytes) -> None:
            nonlocal dac_end
            data.write(payload)
            dac_end = max(loop.time(), dac_end) + block_s

        lead_in = math.ceil(TX_START_SILENCE_S / block_s)
        # Twice the burst's own length, so host stalls end it only when far beyond normal.
        budget = 2 * (TX_PREROLL_BLOCKS + lead_in + blocks + TX_FLUSH_BLOCKS + 1) * block_s
        budget += 2 * self.config.timeout + 1
        try:
            try:
                async with asyncio.timeout(budget):
                    for _ in range(lead_in):
                        queue(silence)
                        await data.drain()
                    while self.blocks_sent < blocks and not self.stop_requested.is_set():
                        if control_reader.at_eof() or control_reader.exception() is not None:
                            raise ConnectionError("TX control lost; the server has retuned")
                        ahead = dac_end - loop.time()
                        if ahead >= lead_s:
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(
                                    self.stop_requested.wait(), max(0.0005, ahead - lead_s)
                                )
                            continue
                        if blocks == 1:
                            payload = single
                        elif self.blocks_sent == 0:
                            payload = rising
                        elif self.blocks_sent == blocks - 1:
                            payload = falling
                        else:
                            payload = block
                        queue(payload)
                        await data.drain()
                        self.blocks_sent += 1
                    if self.stop_requested.is_set() and 0 < self.blocks_sent < blocks:
                        # Still within the burst's block budget, and ends without a hard edge.
                        queue(falling)
                        await data.drain()
                        self.blocks_sent += 1
                    outcome = "stopped" if self.stop_requested.is_set() else "completed"
                    for _ in range(TX_FLUSH_BLOCKS):
                        queue(silence)
                    await data.drain()
            except (OSError, TimeoutError) as error:
                outcome = f"failed: {type(error).__name__}"
                if data.transport is not None:
                    data.transport.abort()
            await asyncio.sleep(max(0.0, dac_end - loop.time()) + fifo_s)
            if self.ptt_high:
                control.write(PTT_LOW)
                await asyncio.wait_for(control.drain(), self.config.timeout)
        except (OSError, TimeoutError) as error:
            outcome = f"failed: {type(error).__name__}"
        except asyncio.CancelledError:
            # Shutdown or Ctrl-C. Drop what the host still holds, then keep TX control until the
            # board's queue has played, or closing it would move that tone to 600 kHz. A second
            # cancellation ends the wait.
            outcome = "aborted"
            if data.transport is not None:
                data.transport.abort()
            try:
                await asyncio.sleep(max(0.0, dac_end - loop.time()) + fifo_s)
            finally:
                raise
        finally:
            self.outcome = outcome
            self.ptt_high = False
            # Both closes happen before any await, so cancellation cannot keep one open.
            data.close()
            control.close()
            self.closed_at = loop.time()
            await asyncio.gather(_wait_writer_closed(data), _wait_writer_closed(control))

    async def stop(self) -> None:
        """Ask the burst to end and wait for it. Cancelling this wait leaves the burst ending."""
        self.stop_requested.set()
        if self.task is not None:
            await asyncio.wait({self.task})


# Nominal Zynq-7000 rail voltages (V) and an accepted band; values outside are reported, not hidden.
_RAIL_NOMINAL = {
    "vccint": 1.0,
    "vccbram": 1.0,
    "vccpint": 1.0,
    "vccaux": 1.8,
    "vccpaux": 1.8,
    "vccoddr": 1.5,
}
_RAIL_TOLERANCE = 0.1  # +-10 % "in_nominal_range" flag (DDR3L boards run VCCO_DDR at 1.35 V)
_HEALTH_KEYS = re.compile(
    r"^(temp_(raw|offset|scale)|(vccint|vccaux|vccbram|vccpint|vccpaux|vccoddr)_(raw|scale)"
    r"|uptime_s|loadavg|eth0_(rx|tx)_(dropped|errors))$"
)


def parse_board_health(text: str) -> dict[str, Any]:
    """Parse BOARD_HEALTH_COMMAND output. Remote text is untrusted: unknown keys, duplicate keys
    and non-numeric values are rejected; physically implausible values are flagged."""
    values: dict[str, str] = {}
    for line in text.strip().splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or not _HEALTH_KEYS.match(key) or key in values or len(value) > 64:
            return {
                "available": False,
                "reason": f"unexpected line in board-health output: {line[:40]!r}",
            }
        values[key] = value.strip()
    try:
        out: dict[str, Any] = {"available": True}
        if {"temp_raw", "temp_offset", "temp_scale"} <= values.keys():
            raw, off, scale = (float(values[k]) for k in ("temp_raw", "temp_offset", "temp_scale"))
            celsius = (raw + off) * scale / 1000.0
            out["die_temperature_c"] = round(celsius, 3) if -40.0 <= celsius <= 125.0 else None
            if out["die_temperature_c"] is None:
                out["die_temperature_note"] = "outside the Zynq operating range; ignored"
        rails: dict[str, Any] = {}
        for rail, nominal in _RAIL_NOMINAL.items():
            if f"{rail}_raw" in values and f"{rail}_scale" in values:
                volts = float(values[f"{rail}_raw"]) * float(values[f"{rail}_scale"]) / 1000.0
                in_range = abs(volts - nominal) <= _RAIL_TOLERANCE * nominal
                rails[rail] = {"volts": round(volts, 4), "in_nominal_range": in_range}
        out["supply_rails"] = rails
        if "uptime_s" in values:
            out["uptime_s"] = float(values["uptime_s"])
        if "loadavg" in values:
            out["loadavg"] = [float(v) for v in values["loadavg"].split()][:3]
        counters = {k: int(v) for k, v in values.items() if k.startswith("eth0_")}
        if counters:
            out["eth0_counters"] = counters
    except ValueError:
        return {"available": False, "reason": "non-numeric value in board-health output"}
    if "die_temperature_c" not in out and not rails:
        out["available"] = False
        out["reason"] = "XADC device not found"
    return out


async def read_board_health(config: Settings) -> dict[str, Any]:
    """Temperature, supply rails, uptime, load and eth0 counters via one read-only SSH command."""
    if config.mode != "live" or not config.ssh_user:
        return {"available": False, "reason": "Optional read-only SSH disabled; set RP_SSH_USER"}
    try:
        result = await _ssh_command(config, BOARD_HEALTH_COMMAND)
    except (OSError, TimeoutError, ValueError):
        return {"available": False, "reason": "SSH unavailable, timed out or response too large"}
    if not result.get("available"):
        return {"available": False, "reason": result.get("reason", "SSH command failed")}
    parsed = parse_board_health(str(result.get("text", "")))
    parsed["note"] = (
        "Die temperature is a proxy for board temperature, not the crystal itself. "
        "Remote text is untrusted diagnostic data; MAC and DNA are never read."
    )
    return parsed
