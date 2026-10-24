# RedPitayaCore-MCP design

Build a local Python MCP stdio server for Claude Code, targeting STEMlab 125-14,
official OS 3.00-57 and Pavel Demin sdr_transceiver 1.0-20260302 (cbecbdd4).
These version values are operator-reported targets, not device measurements.

The accessible user projects were inspected first. Their Red Pitaya backend is
an older SCPI implementation and is incompatible. No working four-socket client
was accessible. Implement only the verified subset of Pavel Demin's C server at
71c3402a; do not copy unrelated private projects or depend on SCPI.

Use the official MCP Python SDK maintenance line, pinned below version 2, and
stdlib asyncio TCP plus httpx (already an SDK dependency). Mock mode is default.
Live mode requires an explicit IPv4 target. MCP callers cannot choose hosts,
URLs, SSH commands, files, registers, or bitstreams.

Passive diagnostics send no TCP bytes; optional SSH runs fixed read-only
commands with existing key authentication and strict host checking. Report OS,
application metadata/process evidence, and FPGA manager state independently.
Unknown or unavailable evidence stays unknown. FPGA state is not image identity.

RX-only connections need an explicit operator configuration flag. Role 0
resets RX tuning/rate on connect and disconnect; role 1 resets the RX FIFO.
Read bounded little-endian float32 I/Q samples. Never connect roles 2/3, emit
PTT commands, transmit samples, expose arbitrary commands, or write registers.
The default build has no hardware TX enable path.

App lifecycle defaults to a plan/dry run. Live start is blocked because starting
the vendor app may program the FPGA, prohibited by this task. Mock lifecycle is
functional. Only source-verified live lifecycle operations may be enabled.
No actual hardware lifecycle/RX operations run during development.

Validate with protocol vectors, loopback TCP/HTTP fixtures, real MCP stdio
client initialization/list/calls, safety failures, lint, type checks, static
security and dependency auditing. Publish only this new repository after
reviewing tracked files and excluding local addresses/credentials/captures.
