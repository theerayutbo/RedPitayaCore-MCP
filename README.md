# RedPitayaCore-MCP

A local, RX-only MCP server for safe Red Pitaya diagnostics and Claude Code.
It targets an operator-managed STEMlab 125-14 running official Red Pitaya OS
3.00-57 with Pavel Demin `sdr_transceiver` 1.0-20260302 build `cbecbdd4`.
Those values are a compatibility target, not facts inferred from an open port or
device response.

The server starts in deterministic mock mode, with no target host and no hardware
traffic. Its live surface is deliberately narrow: bounded metadata and connection
diagnostics, optional fixed read-only SSH diagnostics, and explicitly enabled RX
control/data. It has no TX tool, TX socket, PTT command, arbitrary command or
register interface, bitstream operation, or live app lifecycle action.

## Safety boundary

- `RP_MODE=mock` is the default and requires no hardware.
- Live mode requires an explicit unicast IPv4 `RP_HOST` chosen by the operator.
- Live RX requires `RP_ALLOW_RX=true` at process startup and
  `acknowledge_side_effects=true` on `rp_rx_connect`.
- RX connect and disconnect change device state: role 0 resets tuning/rate,
  role 1 resets the RX FIFO, and closing control resets tuning.
- The upstream TCP protocol has no acknowledgement. `connected` does not prove
  the server accepted a role or setting.
- `RP_ALLOW_LIFECYCLE=true` enables lifecycle execution in mock mode only. Live
  start and stop remain blocked because startup may program the FPGA and stop
  changes global controller ownership.
- All sample values, including mock data, are uncalibrated units.
- This MCP has no TX path and does not force the board's outputs off. Actual
  hardware TX state remains `unknown_not_read`; another client may control it.

Read [the protocol boundary](docs/PROTOCOL.md), [provenance](docs/PROVENANCE.md),
and [security policy](SECURITY.md) before enabling live RX.

## Requirements

- Python 3.11 or newer
- Claude Code with local stdio MCP support
- Optional for live system diagnostics: the system `ssh` client, existing key
  authentication, and a verified `known_hosts` entry

The dependency `mcp>=1.30,<2` is intentional. It uses the official Python SDK
1.x maintenance line and avoids silently adopting an unreviewed major version.

## Install

Clone the public repository and create an isolated environment:

```bash
git clone https://github.com/theerayutbo/RedPitayaCore-MCP.git
cd RedPitayaCore-MCP
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

When using `uv`, install exactly the reviewed lock file included in the clone:

```bash
uv sync --frozen
```

Run the server directly with either entry point:

```bash
.venv/bin/redpitayacore-mcp
# or
.venv/bin/python -m redpitayacore_mcp
```

The stdio transport reserves stdout for MCP protocol messages. Operational logs
must go to stderr; do not wrap the command with scripts that print to stdout.

## Add to Claude Code

For safe mock mode at project scope:

```bash
claude mcp add --scope project --transport stdio \
  -e RP_MODE=mock \
  redpitaya-core -- \
  /absolute/path/to/RedPitayaCore-MCP/.venv/bin/redpitayacore-mcp
```

Environment options must appear before the server name. Replace the absolute
path with your clone location. You can also adapt
[`examples/claude-mcp.json`](examples/claude-mcp.json) as a project `.mcp.json`.
The committed example is mock-only and contains no credentials or local address.

Confirm registration, then ask Claude Code to call `rp_profile` or
`rp_connection_status`. These remain local mock operations.

## Configuration

Configuration belongs to the server process, not MCP tool arguments.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RP_MODE` | `mock` | `mock` or `live` |
| `RP_HOST` | unset | Required explicit unicast IPv4 in live mode |
| `RP_PORT` | `1001` | SDR TCP port |
| `RP_HTTP_PORT` | `80` | HTTP metadata port |
| `RP_TIMEOUT` | `3` | I/O timeout from 0.1 through 10 seconds |
| `RP_ALLOW_RX` | `false` | Operator permission for live RX state changes |
| `RP_ALLOW_LIFECYCLE` | `false` | Permission for mock lifecycle execution only |
| `RP_SSH_USER` | unset | Enables optional fixed read-only SSH diagnostics |
| `RP_SSH_PORT` | `22` | SSH port |

The sample [`.env.example`](.env.example) is documentation; the server reads its
process environment and does not need an environment file. Do not commit a live
address, username, credentials, private key, or captured device output.

For a live board, prefer user scope so target details stay out of a shared
project configuration:

```bash
claude mcp add --scope user --transport stdio \
  -e RP_MODE=live \
  -e 'RP_HOST=<BOARD_IPV4>' \
  -e RP_ALLOW_RX=false \
  redpitaya-core -- \
  /absolute/path/to/RedPitayaCore-MCP/.venv/bin/redpitayacore-mcp
```

Replace `<BOARD_IPV4>` before running that command. Start with RX disabled. This
registration permits safe diagnostics only. If you
later enable live RX, do so in your user-local configuration after completing
hardware acceptance. SSH is optional and unavailable until `RP_SSH_USER` is set;
it uses fixed read-only commands, strict host-key checking, and existing key
authentication. The project does not store keys or passwords.

## MCP tools

| Tool | Behavior |
| --- | --- |
| `rp_profile` | Reports configured mode and operator-reported compatibility target |
| `rp_device_diagnostics` | Reads bounded HTTP application/device metadata |
| `rp_connection_probe` | Connects to the configured SDR port, sends no bytes, and closes |
| `rp_system_diagnostics` | Runs fixed read-only OS/app/process/FPGA SSH checks; unavailable unless configured |
| `rp_app_lifecycle` | Plans `start` or `stop`; `dry_run=true` by default; execution is mock-only |
| `rp_rx_connect` | Opens RX control/data with validated frequency and rate; live use requires both permission gates |
| `rp_rx_read` | Reads bounded little-endian float32 I/Q; defaults to 2,048 samples and 8 previews |
| `rp_rx_disconnect` | Closes RX sockets; closing control resets tuning upstream |
| `rp_connection_status` | Reports local socket state and explicitly unknown hardware TX state |

`rp_rx_read` caps acquisition at 8,192 complex samples and returned previews at
32 samples. A successful probe or connection is reachability evidence, not
device, OS, application, build, FPGA-image, or protocol identity.

## Recommended live acceptance order

1. Keep `RP_ALLOW_RX=false` and run `rp_profile`.
2. Run `rp_device_diagnostics` and `rp_connection_probe`.
3. If separately configured, run `rp_system_diagnostics`; review OS, app/process,
   and FPGA state as independent evidence. Unknown remains unknown.
4. Have the operator start the installed app using their established device
   procedure. This project does not automate bitstream loading.
5. Verify the device model, OS release, installed app build, FPGA state, and
   listening service outside this MCP server.
6. Enable RX only after reviewing the side effects and local RF setup. Call
   `rp_rx_connect` with explicit acknowledgement, make a bounded read, then call
   `rp_rx_disconnect`.

No real hardware was used to develop or test this release. Loopback fixtures and
mock mode can validate software behavior, but hardware compatibility remains an
operator acceptance task.

## Development

Install development dependencies:

```bash
python -m pip install -e '.[dev]'
```

Run the local checks:

```bash
pytest
ruff check .
ruff format --check .
mypy src
bandit -q -r src
pip-audit
python -m build
```

The test suite includes protocol vectors, mock behavior, loopback TCP/HTTP
fixtures, safety failures, and a real MCP stdio client path. CI repeats the
repository checks. These checks do not replace hardware validation.
See [validation evidence and precise limits](docs/VALIDATION.md).

## Project status and limitations

- No real STEMlab was contacted during development.
- No TX path exists, even if environment variables are altered.
- Live app start/stop is always blocked.
- TCP roles and settings have no protocol acknowledgement.
- Metadata and open ports do not guarantee the requested hardware/software
  target.
- I/Q units are uncalibrated.
- Compatibility is limited to the source-verified subset described in
  [docs/PROTOCOL.md](docs/PROTOCOL.md).

## License

MIT. See [LICENSE](LICENSE).
