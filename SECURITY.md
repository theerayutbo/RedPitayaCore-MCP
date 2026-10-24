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
Live diagnostics are serialized, with at most six fixed SSH checks in flight.
TCP and SSH operations have deadlines and bounded diagnostic output. This MCP
has no TX path; hardware TX state is unknown and outputs are not forced off.

The server deliberately provides:

- bounded HTTP metadata reads and connect-without-send TCP probes;
- optional fixed, read-only SSH diagnostics using the system OpenSSH client;
- explicitly enabled RX-only sockets and bounded sample reads; and
- mock-only lifecycle simulation.

The server deliberately excludes:

- TX tools, TX socket roles, PTT commands, and sample transmission;
- arbitrary TCP, HTTP, shell, SSH, register, file, or FPGA operations;
- password or private-key storage;
- disabled SSH host-key verification; and
- live app start/stop, because vendor app startup may program the FPGA and stop
  affects global controller ownership.

`RP_ALLOW_RX=true` is an operator permission, not proof that a requested
frequency is lawful or safe. Use suitable attenuation, filtering, grounding,
and RF operating practices. RX connection setup changes device state even though
it does not transmit RF.

## Supported versions

Security fixes are applied to the current default branch. This early project has
no long-term support branches. The dependency range `mcp>=1.30,<2` is intentional:
it follows the official Python SDK 1.x maintenance line while avoiding an
unreviewed major-version migration. Dependency and static-analysis checks run in
CI, but automated checks do not establish hardware safety or protocol
compatibility.
