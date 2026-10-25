# Provenance and compatibility evidence

RedPitayaCore-MCP was written as a new implementation. No private source was
copied into this repository. One optional unified patch and its offline regression
fixture include the minimum upstream server context needed to test a pinned TX
short-read correction; that copied context retains Pavel Demin's MIT notice.

## Accessible projects inspected first

The accessible user projects and repositories were searched before protocol
implementation, as requested:

- Cortex/CortexRF contained an older Red Pitaya SCPI backend. It is a different
  protocol and was not reused.
- `theerayutbo/redpitaya` contained SCPI-oriented code. It was not used as proof
  of the four-socket SDR protocol.
- `pyhfvrt` contained VITA-oriented work unrelated to this TCP protocol.

The requested working `RedPitayaBackend` implementation with separate RX
control/data and TX control/data sockets was not found in the accessible sources.
Accordingly, this project does not claim equivalence with that unavailable
backend and does not infer commands from its description.

## Primary sources

The implemented binary subset was verified from:

1. Pavel Demin, `projects/sdr_transceiver/server/sdr-transceiver.c`, commit
   [`71c3402a015df6136bdb751e8b1531388b8e4f92`](https://github.com/pavel-demin/red-pitaya-notes/blob/71c3402a015df6136bdb751e8b1531388b8e4f92/projects/sdr_transceiver/server/sdr-transceiver.c).
   This is the source for socket role words, RX control encoding, supported rate
   indexes, connection resets, and little-endian float32 I/Q output.
2. Pavel Demin, target build startup script
   `rp_patchs/rp/sdr_transceiver/bazaar/fpga.sh`, commit
   [`cbecbdd45735a04ad6ccb0094a242a1f820665b8`](https://github.com/pavel-demin/red-pitaya-notes/blob/cbecbdd45735a04ad6ccb0094a242a1f820665b8/rp_patchs/rp/sdr_transceiver/bazaar/fpga.sh).
   This establishes that app startup crosses the FPGA-programming safety
   boundary.
3. Red Pitaya, Bazaar command handler
   `Bazaar/nginx/ngx_ext_modules/ngx_http_rp_module/src/rp_bazaar_cmd.c`, commit
   [`0e46d64396a57752bf97315789d6cc6c9fbddad8`](https://github.com/RedPitaya/RedPitaya/blob/0e46d64396a57752bf97315789d6cc6c9fbddad8/Bazaar/nginx/ngx_ext_modules/ngx_http_rp_module/src/rp_bazaar_cmd.c).
   This is the reference for the Bazaar lifecycle request path and controller
   implications.

A source-file comparison was performed between the requested target build and
the independently implemented `71c3402a` subset before publication. The scope is
still deliberately limited to the documented RX operations.

Both commit trees contain the exact same server Git blob,
`d61e6ba1700f77c3d831fd724893edf2d07dd7dc` (7,657 bytes). This verifies source-file
identity between those upstream revisions; it does not verify the executable on
the user's device. The upstream
[GNU Radio client](https://github.com/pavel-demin/red-pitaya-notes/blob/71c3402a015df6136bdb751e8b1531388b8e4f92/projects/sdr_transceiver/gnuradio/red_pitaya.py)
also verifies little-endian command packing, sample-rate indexes and complex
float32 stream interpretation. It is GPL-licensed, was consulted for protocol
facts, and is not included or copied into this MIT implementation.

## Claims this evidence does not support

The sources do not prove that a particular network host is a STEMlab 125-14,
runs OS 3.00-57, has build `cbecbdd4` installed, has a particular FPGA image
loaded, or accepts settings merely because TCP port 1001 is open. Those are
separate hardware acceptance checks.

The operator-reported target values are compatibility goals. They are not
embedded as discovered device facts, and diagnostics preserve unknown or
unavailable evidence rather than converting it into a positive identification.

## License boundary

This repository is licensed under MIT. Links and small factual protocol
descriptions identify upstream evidence. The full upstream server is not
vendored. The limited upstream context in
[`patches/sdr-transceiver-exact-tx.patch`](../patches/sdr-transceiver-exact-tx.patch)
and `tests/test_tx_server_patch.py` is documented with the upstream commit, blob,
copyright, and MIT license in [`patches/README.md`](../patches/README.md).
Users remain responsible for the licenses and terms of software on their Red
Pitaya device.
