# FPGA development extension

The follow-up request expands software capabilities. Actual board programming,
raw MMIO writes and RF transmission remain outside the currently authorized
execution scope. The user asked how those operations can be made safe; this
release provides concrete preflight plans rather than assuming hardware consent.

## Deliverables

- An operator-selected local project root: bounded RTL/constraint reads,
  cooperative-lock and hash-guarded atomic source replacement, artifact hashes,
  and textual timing/debug report reads. External writers must be quiescent.
- A source-pinned register map and register-operation plans with access widths,
  side effects and explicit unknown image identity. No generic memory access.
- An experimental Vivado workflow for behavioral simulation, synthesis and
  implementation, including bitstream generation to a local file. Its generated
  Tcl requests no hardware-manager or programming action. Real local builds need
  operator configuration and per-call trusted-code acknowledgement; default calls
  return a dry run. Missing tools are reported honestly.
- Generated ILA instrumentation Tcl plans, requiring exact operator-selected
  clock/probe nets; no hardware capture or configuration.
- Detailed fixed-command FPGA/installed-artifact evidence over optional SSH.
- Bitstream/register/TX hardware preflight plans identifying missing evidence,
  exclusive ownership and independent recovery/TX-inhibit requirements.

## Boundaries

Local project operations require RP_PROJECT_ROOT. Source changes additionally
require RP_ALLOW_PROJECT_WRITE. MCP callers cannot choose a remote host,
executable or arbitrary command. Local builds require RP_ALLOW_LOCAL_BUILD and
an operator-configured absolute Vivado executable. Build input source is trusted
local code, copied through POSIX descriptor-anchored staging into a separate run
directory. The generated build script does not source existing project Tcl
hooks. HDL, XDC constraints, simulator features and vendor initialization are
still executable trusted inputs, not an OS sandbox. Windows and real Vivado
execution are not validated in this release.

Actual builds must run on an isolated host or container with no Red Pitaya
network route, USB/JTAG, or other board-device access. The per-call
`acknowledge_trusted_code=true` gate records awareness of the risk but is not a
sandbox or independent approval. Build results cannot determine whether trusted
code caused hardware activity outside the dedicated MCP adapters.

Build jobs are asynchronous and bounded in number, duration and output. Server
shutdown/cancellation terminates owned build process groups. Completion means a
process result, not proof of timing closure, correct logic or hardware safety.

Hardware plans do not execute. Stock application metadata cannot prove active
bitstream identity or guarantee RF output is off. Loading files and emitting RF
need separately reviewed live adapters and actual board/RF interlocks; environment
flags cannot silently enable those actions in this release.
