# RedPitayaCore-MCP

## Core-first PS/PL architecture

Core is the general-purpose software development and device runtime MCP. It owns PS-side Python/C/C++ applications, drivers, DSP, diagnostics and operation of existing PL engines. The optional [RedPitayaFPGA-MCP](https://github.com/theerayutbo/RedPitayaFPGA-MCP) owns RTL, IP, hardware interfaces, simulation, synthesis, implementation and Vivado. Core needs no Vivado for PS development. Core retains read-only FPGA inspection, diagnostics, register and hardware preflight plans. RTL writes, ILA design, simulation and builds are not exposed even if legacy environment flags are set; their implementation is retained for migration only. live loading, arbitrary MMIO and DMA control remain unimplemented.

### PS software workspace

Set `RP_PS_PROJECT_ROOT` to an absolute existing local directory, separate from your FPGA workspace. `RP_ALLOW_PS_PROJECT_WRITE=true` enables bounded local source writes (default false). The `rp_ps_project_info`, `rp_ps_read_source` and `rp_ps_write_source` tools support Python, C, and C++ source/header files. Replacements require the current SHA-256; no source is executed, built, transferred or deployed by these tools. POSIX workspace constraints and the requirement for quiescent external editors still apply. Use a source-only workspace with no secrets. This is a first-stage PS development capability, not remote code execution or a complete PS SDK.


A local MCP server for safe Red Pitaya diagnostics, RX access, and bounded
read-only FPGA inspection from Claude Code.
It targets an operator-managed STEMlab 125-14 running official Red Pitaya OS
3.00-57 with Pavel Demin `sdr_transceiver` 1.0-20260302 build `cbecbdd4`.
Those values are a compatibility target, not facts inferred from an open port or
device response.

The server starts in deterministic mock mode, with no target host and no hardware
traffic. Its live surface is deliberately narrow: bounded metadata and connection
diagnostics, optional fixed read-only SSH diagnostics, and explicitly enabled RX
control/data. The FPGA inspection surface reads local HDL, constraints, artifacts and reports, and provides non-executing register and hardware plans. FPGA design and build belong to the separate FPGA-MCP. No live MMIO, image loading or ILA capture is implemented.

## Official-first integration (phase 1)

Use the vendor's existing SCPI API instead of reimplementing hardware drivers.
`rp_official_capabilities` lists implemented adapters, not detected hardware.
`rp_scpi_identity` and `rp_scpi_acquisition_status` expose fixed queries on TCP 5000.
Mock mode is default and makes no network calls. Live SCPI requires
`RP_ALLOW_SCPI=true`, disabled SDR RX/TX permissions, and per-call
`acknowledge_exclusive_scpi=true` after independently confirming an already-running
official SCPI server, v0.94 image and exclusive access. Never start/stop a service
or switch images automatically. Identity responses do not prove the active image or board model.
The acquisition query checks the SCPI identity response first, then reads decimation, trigger and buffer-fill state sequentially;
it does not start acquisition, read waveform samples or change outputs.
The operator must ensure no other client changes the image or owns the hardware. The SCPI connection is unauthenticated; use only a trusted isolated network.
Direct MMIO, C/Python API, streaming and full instrument control remain future work.

Sources: [official SCPI documentation](https://redpitaya.readthedocs.io/en/latest/appsFeatures/remoteControl/scpi.html),
[official acquisition commands](https://redpitaya.readthedocs.io/en/latest/appsFeatures/remoteControl/command_list/commands-acq.html),
[Release-2026.1 SCPI command table](https://github.com/RedPitaya/RedPitaya/blob/Release-2026.1/scpi-server/src/scpi-commands.cpp).
Live hardware verification of this adapter is pending.

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
- TX is off by default. `rp_tx_start` sends one bounded tone burst and needs
  `RP_ALLOW_TX=true`, an operator band, and `acknowledge_rf_output=true` in that
  call. This emits RF: use a dummy load, a cabled loop or attenuation, and transmit
  on air only where you are licensed to. Amplitude is digital full scale, not power.
- This MCP does not force the board's outputs off outside its own bursts. Actual
  hardware TX state remains `unknown_not_read`; another client may control it.
- Core FPGA workspace is read-only regardless of legacy build/write flags. PS source writes are independently opt-in and do not execute code.
- `rp_fpga_hardware_plan` is always non-executing. No environment variable turns
  bitstream loading, register writes, or its custom-image TX plan into executable
  operations.

Read [the protocol boundary](docs/PROTOCOL.md), [provenance](docs/PROVENANCE.md),
and [security policy](SECURITY.md) before enabling live RX. Use the separate FPGA-MCP for RTL edits and builds.

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
| `RP_ALLOW_TX` | `false` | Operator permission for live RF output |
| `RP_TX_MIN_HZ`, `RP_TX_MAX_HZ` | unset | Band for all RF of a burst. Required with `RP_ALLOW_TX=true` |
| `RP_TX_MAX_AMPLITUDE` | `0.1` | Highest amplitude, as digital full scale up to 1 |
| `RP_TX_MAX_SECONDS` | `10` | Longest burst, up to 180 s (one 110.6 s WSPR transmission with lead and tail) |
| `RP_TX_LEAD_S` | `0.1` | How far TX runs ahead of the DAC, 0.05 to 1 s. Raise it on a loaded host |
| `RP_RX_SAMPLE_ORDER` | `glibc_memcpy` | Restores RX sample order, as needed on the tested board. `raw` is for images that read the FIFO in order |
| `RP_ALLOW_LIFECYCLE` | `false` | Permission for mock lifecycle execution only |
| `RP_SSH_USER` | unset | Enables optional fixed read-only SSH diagnostics |
| `RP_SSH_PORT` | `22` | SSH port |
| `RP_PROJECT_ROOT` | unset | Absolute existing local FPGA workspace directory |
| `RP_PS_PROJECT_ROOT` | unset | Absolute existing PS software workspace |
| `RP_ALLOW_PS_PROJECT_WRITE` | `false` | Enables bounded PS source writes |

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

The server exposes 28 tools: 12 device tools, 3 PS workspace tools, 10 read-only FPGA inspection tools and 3 official API tools.

### Device and RX tools

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
| `rp_tx_start` | Sends one self-ending tone burst, optionally keying PTT. Emits RF |
| `rp_tx_status` | Reports the local burst state and last outcome, with no RF readback |
| `rp_tx_stop` | Ends a burst early, flushes zeros and lowers PTT |

### FPGA inspection tools (read-only)

| Tool | Behavior |
| --- | --- |
| `rp_fpga_capabilities` | Reports read-only inspection capability and explicit hardware limits |
| `rp_fpga_diagnostics` | Reads the same fixed SSH FPGA/overlay/installed-file evidence without MMIO |
| `rp_fpga_register_map` | Shows the source-pinned instance-1 register profile; performs no access |
| `rp_fpga_register_plan` | Validates one named, width-limited read/write proposal; never executes it |
| `rp_fpga_hardware_plan` | Lists blockers and interlocks for image load, register write, or TX burst |
| `rp_fpga_project_info` | Lists bounded HDL, XDC, and artifact paths below `RP_PROJECT_ROOT` |
| `rp_fpga_read_source` | Reads bounded UTF-8 RTL/XDC and returns its SHA-256 |
| `rp_fpga_inspect_artifact` | Hashes a bounded `.bit`, `.bin`, `.dtbo`, `.ltx`, `.rpt`, or `.dcp` file |
| `rp_fpga_read_report` | Reads bounded `.rpt` or `.ltx` text as untrusted output |
| `rp_fpga_manifest` | Binds local hashes to a claimed part/source revision in an unapproved proposal |

Artifact inspection checks extension, size, and SHA-256 only. It does not parse
the format or prove the target board. A manifest is not signed or approved, and
its claimed source revision is not verified against the artifact. A successful
Vivado process does not by itself establish timing closure, correct logic, board
compatibility, or safe RF behavior.

Source replacement uses an advisory lock for cooperating RedPitayaCore-MCP
processes and an expected SHA-256. External editors and build tools must be
quiescent during the write; this is not a filesystem-wide compare-and-swap.

For FPGA development, use the separate FPGA-MCP with its operator permissions and isolation requirements.

`rp_rx_read` caps acquisition at 8,192 complex samples and returned previews at
32 samples. A successful probe or connection is reachability evidence, not
device, OS, application, build, FPGA-image, or protocol identity.

## Recommended live acceptance order

1. Keep `RP_ALLOW_RX=false` and run `rp_profile`.
2. Run `rp_device_diagnostics` and `rp_connection_probe`.
3. If separately configured, run `rp_system_diagnostics`; review OS, app/process,
   and FPGA state as independent evidence. Unknown remains unknown.
4. Have the operator start the installed app using their established device
   procedure. This project does not automate bitstream loading. Do not open the
   board's web UI or call any `/bazaar` URL while the app is in use: on the tested
   board `/bazaar?apps` and `/bazaar?help` stopped the running app within 0.5 s.
5. Verify the device model, OS release, installed app build, FPGA state, and
   listening service outside this MCP server.
6. Enable RX only after reviewing the side effects and local RF setup. Call
   `rp_rx_connect` with explicit acknowledgement, make a bounded read, then call
   `rp_rx_disconnect`.

**Tested on real hardware.** Release 0.2.1 passed 63 of 63 acceptance checks on a
STEMlab 125-14 v1.0 running Red Pitaya OS 3.00-57 and `sdr_transceiver` 1.0-20260302 on
2026-11-02: HTTP and SSH diagnostics, TCP probe, RX at all six sample rates, disconnect
and reconnect, and four safety refusals. It also passed 134 of 134 extended checks: the
local FPGA tools, sustained RX up to 100,000 S/s, and recovery from failures. Above that
rate, reads work but continuity is not guaranteed. Real Vivado builds were not tested. See [docs/VALIDATION.md](docs/VALIDATION.md) for the results
and limits. To repeat them on your own idle board, run `scripts/live_acceptance.py` and
`scripts/extended_acceptance.py`.

## Offline FPGA inspection quick start

Set `RP_PROJECT_ROOT` to an absolute HDL workspace and run Core in mock mode. Use `rp_fpga_project_info`, `rp_fpga_read_source`, `rp_fpga_read_report` and `rp_fpga_inspect_artifact`. For source changes, simulation or Vivado builds, use [RedPitayaFPGA-MCP](https://github.com/theerayutbo/RedPitayaFPGA-MCP).

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

- Hardware results come from one STEMlab 125-14. See the tested-on-hardware note.
- TX is limited to self-ending tone bursts on the stock app. There is no sample
  upload, no modulation and no calibrated power.
- Live app start/stop is always blocked.
- Live FPGA programming, MMIO/register access and ILA capture remain
  unimplemented. Hardware-plan tools cannot enable them.
- The generated Vivado flow is experimental. Vivado was not installed in the
  development environment. Toolchain tests cover generated Tcl and bounded
  fixture processes, not real synthesis, implementation, timing closure,
  bitstream validity, ILA insertion, or board compatibility.
- Configured local FPGA workspaces/builds require POSIX descriptor features.
  Default mock startup does not require a workspace; Windows operation is unvalidated.
- HDL, constraints, and testbenches passed to a build are trusted local code, not
  an OS sandbox. The generated flow does not source existing project Tcl hooks
  or request hardware programming, but other trusted code run by Vivado can use
  the build process's OS, network, and device permissions.
- TCP roles and settings have no protocol acknowledgement.
- Metadata and open ports do not guarantee the requested hardware/software
  target.
- I/Q units are uncalibrated.
- Compatibility is limited to the source-verified subset described in
  [docs/PROTOCOL.md](docs/PROTOCOL.md).

## License

MIT. See [LICENSE](LICENSE).
