# FPGA extension implementation plan

**Goal:** Add useful FPGA project/build/diagnostic tools while preserving the
explicit boundary against actual board programming, raw writes and RF output.
**Spec:** [fpga-extension-design.md](fpga-extension-design.md).
**Architecture:** Local workspace + bounded Vivado jobs + source-pinned profile,
registered beside the existing SDR tools. No new package dependencies.

## Review focus

Project traversal/symlink escapes; stale overwrite hashes; Tcl injection;
unbounded subprocesses and cancellation; simulated/planned evidence mistaken for
observed hardware results. Tests cover each boundary before implementation.

- [x] Add and test a root-constrained workspace with atomic hash-guarded source
  writes, streaming artifact SHA-256 and bounded source/report access.
- [x] Verify primary register/build/toolchain sources; add FPGA register and
  hardware-operation plans without live execution.
- [x] Add and test generated Vivado scripts, input snapshots and bounded local
  build jobs. Detect missing toolchains without claiming successful synthesis.
- [x] Register the new MCP tools and configuration; exercise a real stdio
  client and preserve existing RX/lifecycle safety tests.
- [x] Update examples/docs/security and validation; independently review and
  run all local checks.

Publication verification is recorded by the published revision and its GitHub
Actions result. Vendor/device acceptance remains outside the validated scope.
