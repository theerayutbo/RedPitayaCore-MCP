"""Bounded, fixed-target I/O. No generic remote command or HTTP mutation API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import Any

import httpx

from .config import Settings
from .protocol import MAX_SAMPLES, decode_iq, rx_frequency, rx_rate, rx_role
from .rx_order import BLOCK_BYTES, restore_block

# Any GET under /bazaar reaches the board's app controller. Measured on a STEMlab 125-14,
# /bazaar?help and /bazaar?apps stop a running sdr_transceiver within 0.5 s. Never add one here.
HTTP_PATHS = frozenset({"/sdr_transceiver/info/info.json"})
MAX_RESPONSE = 65536
# Measured on a STEMlab 125-14: reconnecting 0 ms after close was dropped, 50 ms worked.
RX_REOPEN_GUARD_S = 0.2
# Tuning settles before the data role resets the FIFO.
RX_SETTLE_S = 0.05
SSH_COMMANDS = {
    "os_release": "cat /etc/os-release",
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


async def _ssh_command(config: Settings, command: str) -> dict[str, Any]:
    host = _require_live(config)
    if command not in SSH_COMMANDS.values() or not config.ssh_user:
        raise ValueError("SSH requires an allowlisted command and RP_SSH_USER")
    # Fixed argv, no local shell. Remote command is a literal from SSH_COMMANDS.
    process = await asyncio.create_subprocess_exec(
        "ssh",
        "-F",
        "/dev/null",
        "-T",
        "-n",
        "-p",
        str(config.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        f"ConnectTimeout={max(1, int(config.timeout))}",
        f"{config.ssh_user}@{host}",
        command,
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
        host = _require_live(self.config)
        writer = None
        try:
            async with asyncio.timeout(self.config.timeout):
                reader, writer = await asyncio.open_connection(
                    host, self.config.port, family=socket.AF_INET
                )
                writer.write(rx_role(role))
                await writer.drain()
            return reader, writer
        except BaseException:
            await _close_writer(writer)
            raise

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
        if type(count) is not int or not 1 <= count <= MAX_SAMPLES:
            raise ValueError("sample_count must be an integer from 1 to 8192")
        if not self.connected or self.reader is None:
            raise ConnectionError("No RX session; call rp_rx_connect first")
        try:
            async with asyncio.timeout(self.config.timeout):
                raw = await self._read_bytes(self.reader, count * 8)
            return decode_iq(raw)
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
