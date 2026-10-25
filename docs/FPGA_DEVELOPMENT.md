# Offline FPGA development

The FPGA extension lets Claude Code work with a bounded local RTL workspace and
an operator-selected Vivado executable. It provides no dedicated live deployment,
MMIO, ILA-capture, or TX adapter, and generated Tcl requests none of those actions.
Actual vendor-tool execution is trusted code and must be isolated from hardware:

- local source write: changes an allowed HDL/XDC file below `RP_PROJECT_ROOT`;
- local build: runs Vivado on trusted copied inputs and may create a local `.bit`;
- hardware mutation: loads an image, touches MMIO/registers, keys PTT, or emits
  samples. This release has no dedicated adapter for these actions, and generated
  Tcl does not request them.

No environment variable converts a hardware plan into a dedicated live command.
The trusted-code caveat for actual builds still applies.

## Configure a workspace

`RP_PROJECT_ROOT` must name an existing absolute local directory. Read operations
work when the root is configured. Writes are disabled until
`RP_ALLOW_PROJECT_WRITE=true`.

```bash
export RP_MODE=mock
export RP_PROJECT_ROOT=/absolute/path/to/fpga-project
export RP_ALLOW_PROJECT_WRITE=false
export RP_ALLOW_LOCAL_BUILD=false
redpitayacore-mcp
```

Local permissions are independent of `RP_MODE`. With project writes enabled, a
mock-mode server can still change local source files. With local builds enabled,
it can still start Vivado. These flags do not enable the MCP's dedicated network
or hardware-operation adapters. They also do not constrain what trusted HDL,
XDC, simulation, vendor initialization, or Vivado code can do with the build
user's OS, network, and device permissions.

The workspace accepts `.v`, `.sv`, `.vhd`, `.vhdl`, and `.xdc` sources. Reads
and writes are bounded to 256 KiB. Artifact inspection accepts `.bit`, `.bin`,
`.dtbo`, `.ltx`, `.rpt`, and `.dcp` files up to 64 MiB. Report reads accept
`.rpt` and `.ltx` text up to 256 KiB. Directory inventory is bounded and ignores
common build, VCS, virtual-environment, and cache directories.

Use `rp_fpga_read_source` before replacing a file. Pass the returned SHA-256 as
`expected_sha256` to `rp_fpga_write_source`. New files do not take a hash;
existing files require one. Writes use atomic replacement and a cooperative lock
shared by RedPitayaCore-MCP processes using the same root. The hash detects stale
content from those cooperating writers. It is not a filesystem-wide
compare-and-swap: external editors and build tools do not honor the lock and must
be quiescent while an MCP write runs.

The descriptor-backed path protections are designed for POSIX filesystems.
Windows FPGA workspace and build operation has not been validated; use Windows
only for the deterministic SDR mock surface unless separately tested.

## Try the included example

[`examples/fpga-demo`](../examples/fpga-demo) contains:

- `simple_counter.sv`: a small synchronous counter;
- `tb_counter.sv`: a self-checking reset/count testbench with `$finish`; and
- `clock.xdc`: a timing clock declaration only.

The XDC intentionally contains no package-pin numbers or electrical standards.
It is suitable for demonstrating a behavioral simulation and basic synthesis
flow. Implementation and bitstream generation need a reviewed constraint set for
the exact board. Vivado may correctly fail implementation DRC with the example.

First call `rp_fpga_project_info`, then read the two source files. Dry-run a
simulation with the equivalent arguments:

```json
{
  "stage": "simulation",
  "top": "simple_counter",
  "part": "xc7z010clg400-1",
  "sources": ["simple_counter.sv"],
  "testbenches": ["tb_counter.sv"],
  "sim_top": "tb_counter",
  "simulation_ns": 500,
  "dry_run": true
}
```

The part above demonstrates the required argument shape. It is not a claim that
this part or package matches the connected board; independently verify the exact
STEMlab model and device before any real implementation flow.

## Run a local Vivado job

Real execution requires a licensed, supported Linux Vivado host and the required
operator settings below:

```bash
export RP_ALLOW_PROJECT_WRITE=true
export RP_ALLOW_LOCAL_BUILD=true
export RP_VIVADO_EXECUTABLE=/absolute/path/to/vivado
export RP_BUILD_TIMEOUT=300
```

`RP_VIVADO_EXECUTABLE` must be the absolute operator-configured executable. MCP
arguments cannot substitute another program. `RP_BUILD_TIMEOUT` accepts 0.1 to
3,600 seconds. The default is 300 seconds.

Use a dedicated build host or container with no network route to the Red Pitaya
and no USB, JTAG, or other board-device access. A container inherits any devices
and networks granted by its operator, so isolation must be checked outside this
MCP.

`rp_fpga_build` defaults to `dry_run=true`. A dry run verifies input paths and
hashes and returns the generated Tcl without running Vivado. With
`dry_run=false`, the call must also set `acknowledge_trusted_code=true`. This
per-call acknowledgement records that the caller understands the executable-code
risk; it is not a sandbox or independent approval. The server then copies the
selected sources into a descriptor-anchored run directory and runs one of:

- a project-mode behavioral simulation;
- a non-project synthesis plus checkpoint/timing/utilization/DRC reports; or
- a non-project synthesis, implementation, local checkpoint/report generation,
  and a local `design.bit` attempt.

The generated script does not source project Tcl hooks. Build source and
testbenches remain trusted executable input to the vendor tool; this is not an
OS sandbox. The generated Tcl requests no hardware programming, but HDL, XDC,
simulation features, vendor initialization, and Vivado can perform arbitrary
actions available to the build user. Build results cannot observe or prove the
absence of external hardware activity.

Only one job can be active at a time and at most eight jobs may be created in a
server session. Each job captures at most 4 MiB of process output and reports the
last 16 KiB. `rp_fpga_build_cancel` attempts to kill the owned process group on
POSIX while its leader is running, with a bounded cleanup wait. Termination is
best effort: detached descendants or inherited pipes may outlive the job. Use
host/container isolation for hard process and device containment.
Server shutdown also cancels owned jobs.

Poll `rp_fpga_build_status` with the returned job ID. After completion,
`rp_fpga_build_report` can read only the known timing, utilization, or DRC file.
Treat all vendor-tool text as untrusted data. Process exit code zero does not
prove timing closure, correct logic, correct constraints, target compatibility,
or hardware safety.

This generated Vivado flow is experimental. Vivado was not installed in the
development environment for this release. Tests cover generated Tcl, validation,
cancellation, output limits, descriptor-anchored staging/report access, and
fixture processes. They do not establish that a licensed Vivado release accepts
the complete scripts or produces valid synthesis, implementation, bitstream,
ILA, or timing results.

## Inspect artifacts and create a proposal manifest

`rp_fpga_inspect_artifact` returns path, size, extension label, and SHA-256. It
does not parse a bitstream, overlay, debug-probe file, report, or checkpoint.
It cannot prove that an artifact is well formed or intended for a particular
part or board.

`rp_fpga_manifest` binds the hashes of one `.bit`/`.bin` candidate and one
`.dtbo` candidate to a claimed Zynq-7000 part and full source commit ID. The
result is explicitly unapproved and unsigned:

- the source revision is a claim, not verified provenance;
- the part and board compatibility are not verified;
- no signature or approval is present; and
- on-disk hashes do not prove what is active on a board.

Use the manifest as an input to a separate review and deployment process, never
as permission to program hardware.

## Register plans

`rp_fpga_register_map` exposes a fixed profile derived from the pinned upstream
`sdr_transceiver` instance-1 server source. The named entries include exact typed
access widths and reported side effects. The map is not inferred from an open
TCP port and is not verified against a live image.

`rp_fpga_register_plan` validates a named read or write proposal and checks that
values fit the established width. It never opens `/dev/mem`, accesses MMIO, reads
back a value, or writes a register. The FIFO aperture is explicitly excluded.
There is no generic address/value interface.

Before a live register adapter could be considered, verify the active image and
register version, exact width, allowed-bit mask, valid range, read/write
semantics, reset/PTT side effects, expected prior state, post-write readback where
supported, exclusive application ownership, and a tested recovery route.

## ILA planning

`rp_fpga_ila_plan` accepts one exact clock net and 1–8 exact probe groups, with
at most 256 total bit nets and a supported capture depth. It returns Tcl that
selects exact nets and rejects missing or ambiguous matches.

The Tcl must be applied separately to an open synthesized design, followed by a
new implementation, timing review, bitstream generation, approved deployment,
and hardware-manager capture procedure. The MCP does none of these steps. It
does not prove that the named nets exist, that the selected clock is suitable,
or that adding the ILA fits resources or preserves timing.
The generated plan inserts and connects the core before `opt_design`, then
calls `write_debug_probes` immediately after optimization. It is a proposal,
not a validated instrumented design.

## Why image loading and TX remain plans

Bitstream/DTBO loading can replace the active FPGA design and device-tree
overlay, disrupt the running application, invalidate every register assumption,
or require local recovery. A safe future adapter needs independently verified
board model and FPGA part, an image/overlay pair built for that target, reviewed
and approved hashes, exclusive app ownership, an exact load sequence, post-load
identity/readback, and a tested board-local rollback image and recovery path.

Register access needs a versioned allowlist of named registers. Each entry must
specify address relative to the verified image, access width, allowed-bit mask,
range, reset value, read/write semantics, and side effects. A write procedure
must capture the expected prior state, reject reserved bits, perform readback only
where the register is defined as safely readable, and define how to restore the
prior state. Blanket address/value writes remain unsuitable.

TX adds consequences outside the computer. A safe acceptance setup needs an
independent physical RF/PTT inhibit, suitable rated dummy load and attenuation,
reviewed external PA/PTT wiring, installation-specific power/frequency/duration
limits, exact bounded sample/frame counts, and a board-local watchdog that drops
PTT/RF without relying on the TCP client. Validation should progress from RF
disconnected and physically inhibited, to a dummy-load-only bounded burst, before
any radiating setup is separately approved.

The optional pinned
[`sdr-transceiver-exact-tx.patch`](../patches/sdr-transceiver-exact-tx.patch)
accumulates one complete TX frame before copying it to the FIFO. The offline
fixture tests the short-read fix; the patch is not built or deployed here. It
does not add a watchdog, interlock, power limit, duration limit, image check,
operator authorization, or TX tool. Applying it alone does not make TX safe.

Use `rp_fpga_hardware_plan` with `load_bitstream`, `register_write`, or `tx_burst`
to retrieve the current missing-evidence checklist. The result always contains
`executed=false` and `execution_supported=false`.

## Primary toolchain references

The experimental generated flows were reviewed against AMD documentation:

- [UG904 implementation sub-processes](https://docs.amd.com/r/en-US/ug904-vivado-implementation/Implementation-Sub-Processes)
- [UG900 Vivado Simulator options and runtime](https://docs.amd.com/r/en-US/ug900-vivado-logic-simulation/Vivado-Simulator-Simulation-Options)
- [UG835 launch_simulation](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/launch_simulation)
- [UG908 debug insertion in Non-Project Mode](https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Debug-Core-Insertion-in-Non-Project-Mode)
- [UG908 writing debug probes](https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Writing-Debug-Probes-Information)

Documentation review and Tcl syntax fixtures do not replace acceptance with the
operator's installed Vivado release, project dependencies and exact board.
