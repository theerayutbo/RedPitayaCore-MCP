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
the 12 fixed SSH checks with at most two SSH processes concurrently.
TCP and SSH operations have deadlines and bounded diagnostic output. Hardware
TX state is not read back, and outputs are not forced off when no burst is
running; another client may drive them.

TX is off unless the operator sets `RP_ALLOW_TX=true` and a band with
`RP_TX_MIN_HZ` and `RP_TX_MAX_HZ`. Each burst is one tone, bounded by
`RP_TX_MAX_AMPLITUDE` and `RP_TX_MAX_SECONDS`, and needs
`acknowledge_rf_output=true` in that call. There is no streaming, sample upload
or open-ended carrier. The limits are software limits in this process: they do
not replace a dummy load, attenuation, filtering, licensing or a physical
inhibit, and nothing here measures or calibrates RF power.

Core's FPGA workspace is read-only regardless of legacy environment flags. Bounded inspection covers source, reports, artifact hashes and metadata; hashes do not validate Xilinx format, target, signature or the active image. PS source edits require a separately configured root, explicit operator permission and current SHA-256 for replacements; external editors must be quiescent. Core does not execute PS source or provide a general shell. The separate FPGA-MCP owns RTL edits and builds; actual builds must run on an isolated host without board network or device access.

The server deliberately provides:

- bounded HTTP metadata reads and connect-without-send TCP probes;
- optional fixed, read-only SSH diagnostics using the system OpenSSH client;
- explicitly enabled RX-only sockets and bounded sample reads; and
- mock-only lifecycle simulation;
- bounded local RTL/XDC and report reads, artifact inspection, and opt-in local PS source editing;
- non-executing named-register, hardware-preflight and manifest plans.

The server deliberately excludes:

- TX of client-supplied samples, streaming TX, and open-ended carriers;
- arbitrary TCP, HTTP, shell, SSH, remote file, register, or FPGA operations;
- password or private-key storage;
- disabled SSH host-key verification; and
- live app start/stop, because vendor app startup may program the FPGA and stop
  affects global controller ownership.

No environment switch enables a dedicated RedPitayaCore-MCP bitstream/DTBO,
MMIO/register, hardware-manager, or ILA capture adapter, and no setting makes
`rp_fpga_hardware_plan` execute anything, including its `tx_burst` plan for a
custom FPGA image.
`rp_fpga_hardware_plan` only reports prerequisites. Core exposes no FPGA build or RTL write tool, even with legacy flags enabled.

Before a future live adapter could be reviewed, it would need exact board and
active-image identity, approved bitstream and DTBO hashes, exclusive application
ownership, named register widths/masks/ranges/side effects, and a tested local
recovery path.

The stock server has no watchdog. Measured on one board, OUT falls silent once
samples stop, but a client that stalls with its TX control socket open leaves
the PTT pin high until that socket closes. So do not rely on software to protect
an amplifier keyed by PTT: fit an independent inhibit or hardware timer.

The optional [`patches/sdr-transceiver-exact-tx.patch`](patches/sdr-transceiver-exact-tx.patch)
fixes one pinned upstream short-read data-integrity defect and is tested only on
an offline fixture. It is not deployed by this project. `rp_tx_start` runs on
the unpatched server and avoids the defect only by staying ahead of the board.

`RP_ALLOW_RX=true` is an operator permission, not proof that a requested
frequency is lawful or safe. Use suitable attenuation, filtering, grounding,
and RF operating practices. RX connection setup changes device state even though
it does not transmit RF.

## Official SCPI adapter

SCPI is off by default. Live access requires RP_ALLOW_SCPI and explicit per-call
operator confirmation of the already-running official SCPI service, v0.94
image and exclusive ownership. Live SDR RX/TX permissions cannot be combined
with SCPI in one configuration. Other clients are not interlocked; the operator
must ensure exclusivity. SCPI is unauthenticated and must use a trusted isolated
network. Only fixed query strings are sent, with bounded ASCII CRLF replies
and a total deadline. The identity reply is checked before acquisition queries,
but cannot prove board, firmware, image, or server authenticity. Waveform reads are limited to 256 ASCII/VOLTS samples from an already triggered, filled buffer with keep-arm off; input count and channel are strictly validated. No arbitrary SCPI, reset, acquisition start, service start/stop, MMIO or FPGA loading.

## Supported versions

Security fixes are applied to the current default branch. This early project has
no long-term support branches. The dependency range `mcp>=1.30,<2` is intentional:
it follows the official Python SDK 1.x maintenance line while avoiding an
unreviewed major-version migration. Dependency and static-analysis checks run in
CI, but automated checks do not establish hardware safety or protocol
compatibility.
