# Security policy

## Reporting a vulnerability

Report security issues through GitHub private vulnerability reporting for this
repository. Do not open a public issue for a vulnerability, credentials, private
addresses, device captures, or other sensitive operational details.

For ordinary bugs with no sensitive content, open a GitHub issue with a minimal
mock-mode reproduction. Please remove local paths, usernames, addresses, SSH
material, and device output before posting.

## Security model

RedPitayaCore-MCP is a local stdio server. Claude Code starts it as the current
user, so its process inherits that user's network reach and environment. The
operator controls the target and permissions through process environment
variables; MCP tool arguments cannot select a host, URL, SSH command, register,
file, bitstream, or credential.

The default configuration is `RP_MODE=mock`, has no host, and makes no hardware
requests. Live mode requires an explicit unicast IPv4 address. Treat all live
device responses as untrusted data.

HTTP responses must use identity encoding and are capped before buffering.
Live diagnostic service calls are serialized. Each system-diagnostic call runs
the 11 fixed SSH checks with at most two SSH processes concurrently.
TCP and SSH operations have deadlines and bounded diagnostic output. This MCP
has no TX path; hardware TX state is unknown and outputs are not forced off.

The local FPGA workspace is an operator-selected trust boundary. Paths stay
below one absolute, existing `RP_PROJECT_ROOT`; allowed file types and sizes are
bounded. Replacing an existing source requires the current SHA-256 returned by a
read. Atomic replacement and an advisory lock serialize cooperating
RedPitayaCore-MCP writers. External editors and build tools must be quiescent;
the hash is not a filesystem-wide compare-and-swap or source authentication.
Artifact inspection reports hashes and sizes only: it does not validate Xilinx formats,
the Zynq part, the STEMlab board, signatures, provenance, or the active image.

Real local builds require `RP_ALLOW_PROJECT_WRITE=true`,
`RP_ALLOW_LOCAL_BUILD=true`, and an absolute `RP_VIVADO_EXECUTABLE`. These
permissions are independent of `RP_MODE=mock`: they can change local files and
run Vivado while the MCP's dedicated hardware transport adapters remain disabled.
Every real build call also requires `acknowledge_trusted_code=true`. This is an
explicit hazard acknowledgement, not a sandbox or independent approval. HDL,
XDC, simulation, vendor initialization, and Vivado itself are trusted executable
code and can use the build user's OS, network, and device access. The generated
flow does not source pre-existing project Tcl hooks or request hardware
programming, and MCP arguments cannot select an arbitrary executable or inject
Tcl commands. The local flow relies on POSIX descriptor semantics and remains
experimental because no real Vivado run was available in the development
environment.

Run actual builds on an isolated host or container with no network route to the
Red Pitaya and no USB, JTAG, or other board-device access. Build output and exit
status cannot establish whether trusted code caused external hardware activity.

The server deliberately provides:

- bounded HTTP metadata reads and connect-without-send TCP probes;
- optional fixed, read-only SSH diagnostics using the system OpenSSH client;
- explicitly enabled RX-only sockets and bounded sample reads; and
- mock-only lifecycle simulation;
- bounded local RTL/XDC reads and SHA-guarded writes;
- optional local behavioral simulation, synthesis, implementation, and local
  bitstream generation through one operator-selected Vivado executable; and
- non-executing named-register, hardware-preflight, manifest, and ILA plans.

The server deliberately excludes:

- TX tools, TX socket roles, PTT commands, and sample transmission;
- arbitrary TCP, HTTP, shell, SSH, remote file, register, or FPGA operations;
- password or private-key storage;
- disabled SSH host-key verification; and
- live app start/stop, because vendor app startup may program the FPGA and stop
  affects global controller ownership.

No environment switch enables a dedicated RedPitayaCore-MCP bitstream/DTBO,
MMIO/register, hardware-manager, ILA capture, or TX/PTT adapter.
`rp_fpga_hardware_plan` only reports prerequisites. Operator-enabled trusted
build code retains the host permissions described above; its effects are unmonitored.

Before a future live adapter could be reviewed, it would need exact board and
active-image identity, approved bitstream and DTBO hashes, exclusive application
ownership, named register widths/masks/ranges/side effects, and a tested local
recovery path. TX additionally needs an independent physical inhibit, suitable
dummy load and attenuation, installation-specific power and duration limits,
exact-frame server behavior, and a board-local watchdog. A network disconnect is
not a dependable RF-off mechanism.

The optional [`patches/sdr-transceiver-exact-tx.patch`](patches/sdr-transceiver-exact-tx.patch)
fixes one pinned upstream short-read data-integrity defect and is tested only on
an offline fixture. It is not deployed by this project and does not add a TX
watchdog, interlock, RF limits, authorization, or a TX MCP tool.

`RP_ALLOW_RX=true` is an operator permission, not proof that a requested
frequency is lawful or safe. Use suitable attenuation, filtering, grounding,
and RF operating practices. RX connection setup changes device state even though
it does not transmit RF.

Build jobs permit one active process and eight jobs per server session. They time
out, cap captured output at 4 MiB, retain a 16 KiB tail, and attempt to terminate
the owned process group while its leader runs, with a bounded cleanup wait.
Termination is best effort; detached descendants or inherited pipes may outlive
the job. Host/container isolation must provide hard containment. These controls
do not make untrusted HDL or vendor tools safe.

## Supported versions

Security fixes are applied to the current default branch. This early project has
no long-term support branches. The dependency range `mcp>=1.30,<2` is intentional:
it follows the official Python SDK 1.x maintenance line while avoiding an
unreviewed major-version migration. Dependency and static-analysis checks run in
CI, but automated checks do not establish hardware safety or protocol
compatibility.
