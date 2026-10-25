import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches" / "sdr-transceiver-exact-tx.patch"


UPSTREAM_FIXTURE = r"""
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

volatile uint64_t *fifo;
volatile uint16_t *tx_cntr;
volatile uint8_t *tx_rst;
int sock_thread[4] = {-1, -1, -1, -1};

static int inject_eintr_once;
static int injected_eintr_count;
static ssize_t test_recv(int socket, void *buffer, size_t length, int flags);
#define recv test_recv

void *tx_data_handler(void *arg)
{
  int i, sock_client = sock_thread[3];
  uint8_t buffer[16384];

  *tx_rst &= ~1;
  *tx_rst |= 1;

  while(1)
  {
    while(*tx_cntr > 4096) usleep(500);

    if(*tx_cntr == 0)
    {
      for(i = 0; i < 2048; ++i) *fifo = 0;
    }

    if(recv(sock_client, buffer, 16384, 0) <= 0) break;
    memcpy(fifo, buffer, 16384);
  }

  close(sock_client);
  sock_thread[3] = -1;

  return NULL;
}

#undef recv

static ssize_t test_recv(int socket, void *buffer, size_t length, int flags)
{
  if(inject_eintr_once)
  {
    inject_eintr_once = 0;
    ++injected_eintr_count;
    errno = EINTR;
    return -1;
  }
  return recv(socket, buffer, length, flags);
}

static uint64_t fifo_storage[2048];
static uint16_t tx_counter = 1;
static uint8_t tx_reset;

static void fill_bytes(uint8_t *data, size_t length, uint8_t seed)
{
  size_t i;
  for(i = 0; i < length; ++i) data[i] = (uint8_t)(seed + i * 31u);
}

static void start_handler(int socket, pthread_t *thread)
{
  fifo = fifo_storage;
  tx_cntr = &tx_counter;
  tx_rst = &tx_reset;
  sock_thread[3] = socket;
  if(pthread_create(thread, NULL, tx_data_handler, NULL) != 0) _exit(90);
}

static int send_all(int socket, const uint8_t *data, size_t length)
{
  while(length > 0)
  {
    ssize_t sent = send(socket, data, length, 0);
    if(sent <= 0) return -1;
    data += sent;
    length -= (size_t)sent;
  }
  return 0;
}

static int test_fragmented_complete_then_partial_eof(void)
{
  int sockets[2];
  pthread_t thread;
  uint8_t first[16384], partial[913];
  memset(fifo_storage, 0xa5, sizeof(fifo_storage));
  fill_bytes(first, sizeof(first), 7);
  fill_bytes(partial, sizeof(partial), 19);
  if(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0) return 10;
  inject_eintr_once = 1;
  injected_eintr_count = 0;
  start_handler(sockets[1], &thread);
  if(send_all(sockets[0], first, 37) != 0) return 11;
  usleep(20000);
  if(memcmp(fifo_storage, first, 37) == 0) return 12;
  if(send_all(sockets[0], first + 37, 8000) != 0) return 13;
  if(send_all(sockets[0], first + 8037, sizeof(first) - 8037) != 0) return 14;
  if(send_all(sockets[0], partial, sizeof(partial)) != 0) return 15;
  shutdown(sockets[0], SHUT_WR);
  if(pthread_join(thread, NULL) != 0) return 16;
  close(sockets[0]);
  if(injected_eintr_count != 1) return 17;
  if(memcmp(fifo_storage, first, sizeof(first)) != 0) return 18;
  return 0;
}

static int test_early_eof_does_not_copy(void)
{
  int sockets[2];
  pthread_t thread;
  uint8_t partial[4096], expected[16384];
  memset(expected, 0x5a, sizeof(expected));
  memcpy(fifo_storage, expected, sizeof(expected));
  fill_bytes(partial, sizeof(partial), 3);
  if(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0) return 20;
  inject_eintr_once = 0;
  start_handler(sockets[1], &thread);
  if(send_all(sockets[0], partial, sizeof(partial)) != 0) return 21;
  shutdown(sockets[0], SHUT_WR);
  if(pthread_join(thread, NULL) != 0) return 22;
  close(sockets[0]);
  if(memcmp(fifo_storage, expected, sizeof(expected)) != 0) return 23;
  return 0;
}

int main(void)
{
  int result = test_fragmented_complete_then_partial_eof();
  if(result != 0) return result;
  return test_early_eof_does_not_copy();
}
"""


def test_patch_applies_and_only_copies_complete_tx_frames(tmp_path: Path) -> None:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("a C compiler is required for the upstream patch harness")
    if shutil.which("git") is None:
        pytest.skip("git is required to apply the upstream patch fixture")

    source = tmp_path / "projects" / "sdr_transceiver" / "server" / "sdr-transceiver.c"
    source.parent.mkdir(parents=True)
    source.write_text(textwrap.dedent(UPSTREAM_FIXTURE).lstrip(), encoding="utf-8")

    check = subprocess.run(
        ["git", "apply", "--check", str(PATCH)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
    subprocess.run(["git", "apply", str(PATCH)], cwd=tmp_path, check=True)

    executable = tmp_path / "tx-patch-harness"
    compile_result = subprocess.run(
        [compiler, "-std=c11", "-D_DEFAULT_SOURCE", "-pthread", str(source), "-o", str(executable)],
        capture_output=True,
        text=True,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    run_result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert run_result.returncode == 0, (
        f"harness exited {run_result.returncode}\n"
        f"stdout:\n{run_result.stdout}\n"
        f"stderr:\n{run_result.stderr}"
    )


def test_patch_is_pinned_and_does_not_expand_tx_scope() -> None:
    patch_text = PATCH.read_text(encoding="utf-8")

    assert "cbecbdd45735a04ad6ccb0094a242a1f820665b8" in patch_text
    assert "d61e6ba1700f77c3d831fd724893edf2d07dd7dc" in patch_text
    assert "projects/sdr_transceiver/server/sdr-transceiver.c" in patch_text
    assert "memcpy(fifo, buffer, 16384);" in patch_text
    assert "EINTR" in patch_text
    assert "bitstream" not in patch_text.lower()
    assert "gpio" not in patch_text.lower()
