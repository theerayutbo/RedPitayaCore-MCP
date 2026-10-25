import asyncio
import gzip
import struct
from contextlib import asynccontextmanager

import httpx
import pytest

from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.transport import (
    DeviceHTTP,
    RxConnection,
    _ssh_command,
    probe_tcp,
    read_system,
)


class Payload(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


@asynccontextmanager
async def tcp_server(handler):
    tasks = set()

    async def wrapper(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(wrapper, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield Settings(mode="live", host="127.0.0.1", port=port, timeout=0.2, allow_rx=True)
    finally:
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*list(tasks), return_exceptions=True), 1)


async def test_probe_sends_no_selector():
    received = []

    async def handler(reader, writer):
        received.append(await reader.read())

    async with tcp_server(handler) as config:
        assert (await probe_tcp(config))["reachable"] is True
    assert received == [b""]


async def test_rx_wire_roles_commands_fragmented_iq_and_cleanup():
    received = []
    closed = []

    async def handler(reader, writer):
        role = await reader.readexactly(4)
        received.append(role)
        if role == struct.pack("<I", 0):
            received.append(await reader.readexactly(8))
        elif role == struct.pack("<I", 1):
            payload = struct.pack("<4f", 1, -2, 0.5, 0.25)
            for part in (payload[:3], payload[3:7], payload[7:]):
                writer.write(part)
                await writer.drain()
        else:
            pytest.fail("TX selector sent")
        await reader.read()
        closed.append(True)

    async with tcp_server(handler) as config:
        rx = RxConnection(config)
        await rx.connect(7_100_000, 50_000)
        assert await rx.read(2) == [(1, -2), (0.5, 0.25)]
        await rx.close()
        assert not rx.connected
    assert received == [
        struct.pack("<I", 0),
        struct.pack("<II", 7_100_000, 0x10000001),
        struct.pack("<I", 1),
    ]
    assert len(closed) == 2


@pytest.mark.parametrize("failure", ["eof", "timeout", "nan"])
async def test_rx_bad_stream_cleans_both_sockets(failure):
    async def handler(reader, writer):
        role = struct.unpack("<I", await reader.readexactly(4))[0]
        if role == 0:
            await reader.read()
        elif failure == "eof":
            writer.write(b"abc")
            await writer.drain()
        elif failure == "nan":
            writer.write(struct.pack("<ff", float("nan"), 0))
            await writer.drain()
            await reader.read()
        else:
            await reader.read()

    async with tcp_server(handler) as config:
        rx = RxConnection(config)
        await rx.connect(600_000, 100_000)
        with pytest.raises((ConnectionError, TimeoutError, ValueError)):
            await rx.read(1)
        assert not rx.connected


async def test_http_fixed_paths_no_proxy_redirects_and_size_limit():
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/sdr_transceiver/info/info.json":
            return httpx.Response(200, stream=Payload(b'{"version":"test-only"}'))
        if request.url.query == b"apps":
            return httpx.Response(302, headers={"location": "http://example.com/"})
        return httpx.Response(200, stream=Payload(b"x" * 65537))

    config = Settings(mode="live", host="192.0.2.25")
    http = DeviceHTTP(config, transport=httpx.MockTransport(handler))
    assert await http.get_json("/sdr_transceiver/info/info.json") == {"version": "test-only"}
    with pytest.raises(httpx.HTTPStatusError):
        await http.get_json("/bazaar?apps")
    with pytest.raises(ValueError, match="65536"):
        await http.get_json("/bazaar?help")
    with pytest.raises(ValueError, match="allowlist"):
        await http.get_json("/bazaar?start=sdr_transceiver?type=run")
    assert len(calls) == 3


async def test_disabled_ssh_does_not_launch_process(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("SSH must be opt-in")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    result = await read_system(Settings(mode="live", host="192.0.2.25"))
    assert result["available"] is False


async def test_system_diagnostic_commands_limit_concurrent_ssh_to_two(monkeypatch):
    active, peak = 0, 0

    async def fake_command(config, command):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"available": True, "text": "fixture"}

    monkeypatch.setattr("redpitayacore_mcp.transport._ssh_command", fake_command)
    result = await read_system(Settings(mode="live", host="192.0.2.25", ssh_user="root"))
    assert peak <= 2
    assert result["fpga_image_verified"] is False
    assert "installed_bitstream_sha256" in result["evidence"]


@pytest.mark.parametrize("oversized", [False, True])
async def test_ssh_reads_all_chunks_or_rejects_oversize(monkeypatch, oversized):
    class Process:
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.killed = False

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Process()
    argv = []

    async def launch(*args, **kwargs):
        argv.extend(args)
        return process

    async def feed():
        process.stdout.feed_data(b"first")
        await asyncio.sleep(0.01)
        process.stdout.feed_data(b"x" * (65536 if oversized else 100))
        process.stdout.feed_eof()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(feed())
    config = Settings(mode="live", host="192.0.2.25", ssh_user="root")
    if oversized:
        with pytest.raises(ValueError, match="65536"):
            await _ssh_command(config, "cat /etc/os-release")
        assert process.killed
    else:
        result = await _ssh_command(config, "cat /etc/os-release")
        assert result["text"] == "first" + "x" * 100
    await task
    assert "StrictHostKeyChecking=yes" in argv
    assert "BatchMode=yes" in argv
    assert argv[-1] == "cat /etc/os-release"
    with pytest.raises(ValueError):
        await _ssh_command(config, "cat /etc/shadow")


async def test_cancelling_rx_read_closes_both_owned_sockets():
    async def handler(reader, writer):
        await reader.read()

    async with tcp_server(handler) as config:
        rx = RxConnection(config)
        await rx.connect(600_000, 100_000)
        reading = asyncio.create_task(rx.read(1))
        await asyncio.sleep(0.01)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert not rx.connected


async def test_real_http_loopback_request_uses_only_fixed_metadata_path():
    requests = []

    async def handler(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        body = b'{"version":"loopback-only"}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    async with tcp_server(handler) as config:
        http = DeviceHTTP(Settings(mode="live", host="127.0.0.1", http_port=config.port))
        assert await http.get_json("/sdr_transceiver/info/info.json") == {
            "version": "loopback-only"
        }
    assert requests[0].startswith(b"GET /sdr_transceiver/info/info.json HTTP/1.1")


async def test_cancelled_disconnect_closes_both_writers_before_waiting():
    waiting = asyncio.Event()

    class Writer:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            waiting.set()
            await asyncio.Event().wait()

    rx = RxConnection(Settings())
    data, control = Writer(), Writer()
    rx.data, rx.control = data, control
    closing = asyncio.create_task(rx.close())
    await waiting.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert data.closed and control.closed
    assert not rx.connected


async def test_http_rejects_compression_before_consuming_or_decoding_body():
    consumed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            consumed.append(True)
            yield gzip.compress(b"x" * 10_000_000)

    async def handler(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Body())

    http = DeviceHTTP(
        Settings(mode="live", host="192.0.2.25"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ValueError, match="[Cc]ompressed"):
        await http.get_json("/bazaar?apps")
    assert not consumed
