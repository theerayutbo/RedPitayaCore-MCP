# Optional upstream server patch

`sdr-transceiver-exact-tx.patch` is a narrowly scoped patch for Pavel Demin's
`sdr_transceiver` server. It targets exactly:

- repository: `pavel-demin/red-pitaya-notes`
- commit: `cbecbdd45735a04ad6ccb0094a242a1f820665b8`
- file: `projects/sdr_transceiver/server/sdr-transceiver.c`
- Git blob: `d61e6ba1700f77c3d831fd724893edf2d07dd7dc`

The upstream repository is MIT licensed. The patch carries only the small amount
of upstream context needed by a unified diff; its new receive-loop lines were
written for this project. The regression fixture in `tests/test_tx_server_patch.py`
also contains only the targeted upstream function plus an independently written
offline harness. See the upstream [LICENSE](https://github.com/pavel-demin/red-pitaya-notes/blob/cbecbdd45735a04ad6ccb0094a242a1f820665b8/LICENSE).

## What it changes

The original TX data handler calls `recv()` once and then copies 16384 bytes to
the FPGA FIFO whenever that call returns any positive byte count. TCP is a byte
stream, so a successful call may return fewer than 16384 bytes. In that case the
remaining buffer bytes are stale.

The patch accumulates exactly one 16384-byte frame before the existing
`memcpy()`, retries `recv()` after `EINTR`, and exits without copying when EOF or
another socket error arrives before a frame is complete. It does not change the
control protocol, sample format, FPGA registers, PTT handling, or any MCP tool.

## Apply to a matching upstream checkout

Verify the target before applying:

```sh
git rev-parse HEAD
git hash-object projects/sdr_transceiver/server/sdr-transceiver.c
git apply --check /path/to/RedPitayaCore-MCP/patches/sdr-transceiver-exact-tx.patch
```

The first two commands must report the commit and blob IDs above. Then apply and
build through the upstream project's normal toolchain:

```sh
git apply /path/to/RedPitayaCore-MCP/patches/sdr-transceiver-exact-tx.patch
```

This repository does not apply, install, build, or deploy the patch on a Red
Pitaya. The automated test applies it only to an offline minimal fixture, compiles
that fixture when a C compiler is available, and exercises fragmented delivery,
an interrupted receive, a complete frame, and EOF during a partial frame.

## Safety boundary

This patch is a data-integrity correction, not a complete TX safety system. It
does not add a watchdog, hardware or software interlock, transmit-duration or
power limits, FPGA-image identity verification, output-load detection, emergency
shutdown, authentication, or operator confirmation. Applying it does not make RF
transmission safe and does not authorize operation on any frequency. Keep the
output terminated in a suitable dummy load during controlled validation, retain
an independent hardware means to inhibit PTT/RF output, and follow the equipment
limits and applicable radio regulations for the installation.

## Upstream license notice

The MIT License (MIT)

Copyright (c) 2014-2025 Pavel Demin

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
