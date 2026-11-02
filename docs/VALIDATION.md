# Validation and precise limits

Local v0.2.0 validation on 2026-10-25 used macOS ARM64, Python 3.12.1 and
MCP SDK 1.30.0. No Red Pitaya or licensed Vivado installation was used for the table
below. Hardware results for v0.2.1 are in [Hardware validation](#hardware-validation).

| Check | Result |
| --- | --- |
| Pytest | 136 passed at v0.2.1, including real MCP stdio clients and the 25-tool surface |
| Network fixtures | Loopback TCP and HTTP only; no Red Pitaya contacted |
| Ruff lint and formatting | Passed |
| Strict mypy | Passed for all source files |
| Bandit | No findings in source |
| pip-audit | Zero known dependency vulnerabilities reported; local package excluded below |
| Wheel and source distribution | Built successfully |
| Repository review | No credentials, site IP, captures or private source copied |
| Local build runner | Fixture subprocesses only: input snapshots, path races, acknowledgement, output limit, timeout and cancellation |
| Tcl generation | Real Tcl interpreter with vendor commands stubbed; separate simulation and non-project flows reviewed against AMD docs |
| TX correction patch | Applied to pinned source and compiled offline; fragmentation, EINTR and partial EOF exercised in a C fixture |

The new local project itself is not published on PyPI and was skipped by the
dependency vulnerability database. Its source is covered by review and static
analysis; an audit with no known findings is not a security guarantee.

An independent review identified and verified fixes for cancellation during
disconnect, compressed HTTP response expansion, concurrent diagnostic process
growth, and ambiguous TX state reporting. The v0.2 review additionally covered
descriptor-pinned workspace/build paths, bounded inventory scans, cooperative
writer serialization, trusted-code acknowledgement, Tcl mode separation, ILA
sequencing, final report provenance and process cleanup. Regression tests cover
these boundaries. Actual Vivado commands and device operation remain unvalidated.

GitHub Actions repeats checks on Python 3.11, 3.12 and 3.13, with a separate
dependency audit. Check the repository's Actions page for the current result.
Actions use pinned commit references and read-only repository permissions.

The first published revision `e527bda` passed all four jobs (Python 3.11, 3.12,
3.13 and dependency audit):
[successful CI run](https://github.com/theerayutbo/RedPitayaCore-MCP/actions/runs/40099377661).

## Hardware validation

Release 0.2.1 was tested on a real STEMlab 125-14 on 2026-11-02, from macOS over a
direct Ethernet link. `info.json` reported `SDR transceiver` 1.0-20260302, revision
`cbecbdd4`. The OS version was not read, because SSH was not configured.

`scripts/live_acceptance.py` passed 42 of 42 checks. Tool checks drive the real server
through an MCP stdio client. The 13 signal checks call the same service code in
process, because `rp_rx_read` returns at most 32 preview samples. No TX role was opened and no signal source
was connected for the test. The test tone is the ADC's own DC offset, which appears at
-f when the receiver is tuned to f.

| Capability | Result on the board |
| --- | --- |
| `rp_device_diagnostics` | Read `info.json`. The app stayed up afterwards |
| `rp_connection_probe` | Reachable with the app up. `refused` in this run with it stopped, though the check also accepts `timeout` |
| `rp_rx_connect` safety | Refused without `RP_ALLOW_RX` and without acknowledgement |
| `rp_rx_connect`, `rp_rx_read`, `rp_rx_disconnect` | Reads of 1, 7, 2048 and 8192 samples over two connect cycles |
| `rp_connection_status` | Reported the open session |
| Diagnostics during RX | RX reads continued after HTTP and probe calls in one short check |
| `rp_app_lifecycle` | Dry run did not execute. Live stop was refused |
| `rp_system_diagnostics`, `rp_fpga_diagnostics` | Reported SSH off. SSH itself was not exercised |

| Rate, S/s | Tuned to, Hz | Tone measured, Hz | Worst of 7 fs/8 spurs, dBc |
| --- | --- | --- | --- |
| 20,000 | 2,000 | -2,000.06 | -89.3 |
| 50,000 | 5,000 | -5,000.16 | -96.4 |
| 100,000 | 10,000 | -10,000.32 | -96.7 |
| 250,000 | 25,000 | -25,000.79 | -95.7 |
| 500,000 | 50,000 | -50,001.58 | -84.9 |
| 1,250,000 | 125,000 | -125,003.94 | -83.2 |

With correction off, the same check at 50,000 S/s showed -16.0 dBc, so the test does
detect the defect. Tone frequency confirms tuning and rate at these frequencies only.
The spur figures cover only the seven tone + k * fs/8 frequencies, not all spurs, and
exclude block 0.

Testing on the board found three defects in 0.2.0, now fixed:

- `rp_device_diagnostics` requested `/bazaar` URLs that stopped the running app, so a
  tool marked read-only was not.
- RX returned samples in the wrong order, with no error.
- Reconnecting immediately after disconnect failed.

## What remains unverified

- SSH diagnostics, including `board_model`, process names, FPGA manager state and file
  hashes, have not been run on a board.
- One board on one day is the whole sample. The OS version was not read, and other
  images may need `RP_RX_SAMPLE_ORDER=raw`.
- RX was checked with the ADC's DC offset at low frequencies. RF input at HF, gain,
  noise floor and frequency accuracy were not measured.
- Streaming continuity at the higher rates was not measured. Reads are bounded
  snapshots, and the Python client may not keep up with 1,250,000 S/s.
- Whether diagnostics disturb a running TX or full-duplex session is unknown. Avoid
  calling them during such a session.
- Claude Code's registration instructions were checked against official docs;
  automated integration uses the official SDK's actual stdio client. No local
  Claude Code configuration was changed during development.
- Live SSH authentication, filesystem paths, app metadata availability and
  FPGA manager state require device acceptance. Missing evidence is reported
  unavailable; labels and sysfs state do not establish image identity.
- Live app lifecycle cannot execute. Starting the app can load a bitstream and
  overlay; stopping it acts on the global current controller.
- RX is opt-in and state-changing. It has no acknowledgement, setting readback,
  sample timestamps or continuity guarantees. Socket state and received data
  cannot prove the model, OS, build, image or requested tuning/rate.
- No dedicated TX adapter exists in this MCP. Hardware TX state remains unknown; the server
  does not stop another client, enforce an RF interlock or force outputs off.
- FPGA register and hardware tools produce non-executing plans. Installed-file
  hashes do not identify the active image, approve an artifact or prove compatibility.
- Vivado workflows are experimental. Fixture success does not establish vendor
  syntax acceptance, synthesis/implementation success, simulation correctness,
  ILA insertion, timing closure, pin compatibility or bitstream validity.
- Local builds require POSIX, operator configuration and per-call trusted-code
  acknowledgement. HDL/XDC/simulation/vendor code can access OS, network and
  devices; builds must run isolated from board access. Job results cannot
  observe those effects. Process-group cleanup is best effort, not containment.
- The TX patch only fixes exact-frame reception. It is not deployed, does not
  add a board-local watchdog and does not establish safe RF output.
- Samples are uncalibrated application units; no voltage, power, ADC clipping,
  RF performance or regulatory compliance claim is made.
