# Validation and precise limits

Local v0.2.0 validation on 2026-10-25 used macOS ARM64, Python 3.12.1 and
MCP SDK 1.30.0. No Red Pitaya or licensed Vivado installation was used for the table
below. Hardware results for v0.2.1 are in [Hardware validation](#hardware-validation).

| Check | Result |
| --- | --- |
| Pytest | 142 passed at v0.2.1, including real MCP stdio clients and the 25-tool surface |
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

Release 0.2.1 was tested on a real board on 2026-11-02, from macOS over a direct
Ethernet link. Read from the board: `STEMlab 125-14 v1.0`, Red Pitaya OS 3.00-57
(Ubuntu 24.04.4), SDR bundle `cbecbdd4`, and `SDR transceiver` 1.0-20260302.

`scripts/live_acceptance.py --ssh-user root` passed 63 of 63 checks. Tool checks drive the real server
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
| `rp_system_diagnostics` | All 12 fixed commands ran over key login with a pinned host key. No MAC or DNA returned |
| `rp_fpga_diagnostics` | Ran over SSH. Reported that no registers were read. FPGA state `operating` |

| Rate, S/s | Tuned to, Hz | Tone measured, Hz | Worst of 7 fs/8 spurs, dBc |
| --- | --- | --- | --- |
| 20,000 | 2,000 | -2,000.06 | -100.2 |
| 50,000 | 5,000 | -5,000.16 | -102.3 |
| 100,000 | 10,000 | -10,000.32 | -93.5 |
| 250,000 | 25,000 | -25,000.79 | -87.2 |
| 500,000 | 50,000 | -50,001.58 | -84.9 |
| 1,250,000 | 125,000 | -125,003.94 | -86.0 |

With correction off, the same check at 50,000 S/s showed -16.0 dBc, so the test does
detect the defect. Tone frequency confirms tuning and rate at these frequencies only.
The spur figures cover only the seven tone + k * fs/8 frequencies, not all spurs, and
exclude block 0.

`scripts/extended_acceptance.py --ssh-user root` passed 134 of 134 checks on the same
board and day: every tool except real Vivado jobs, through the real server where a check
calls a tool. Local tools ran on a copy of `examples/fpga-demo` and on image files copied
from the board. RX was the only radio traffic.

| Area | Result |
| --- | --- |
| Local tools | `rp_profile`, `rp_fpga_capabilities`, project, source, report, artifact and manifest tools worked, and refused traversal, links, stale hashes and bad types |
| Register map | All 11 entries match the server source at the pinned revision. Not compared with the live image, because nothing reads registers |
| Plans | Register, hardware and ILA plans do not execute. Real Tcl parsed the ILA script with vendor commands stubbed |
| `rp_fpga_build` | Dry runs and refusals only. Status, cancel and report refused an unknown job |
| Real image files | Local hashes of `fpga.bit.bin` and `fpga.dtbo` matched `rp_system_diagnostics` |
| Tuning | Eight points from 100 Hz to 600 kHz, tone within 0.01 % or two FFT bins. Worst fs/8 spur -64.3 dBc at 600 kHz, -83.2 dBc or better elsewhere |
| Limits | 1 Hz and 60 MHz read samples. Out-of-range inputs were refused and left the session usable. At 60 MHz only the read was checked |
| Sustained RX | 20,000, 50,000 and 100,000 S/s each kept real time for 20 s with no lasting phase jump, also with diagnostics repeating at 100,000 S/s |
| Churn | 60 connect, read and disconnect cycles with no failure |
| Failure handling | Recovered from a killed client and from the app stopping mid-session. A wrong host and a closed SSH port were reported within limits |
| Second client | Refused at connect, and the first client kept reading |

Claude Code also called `rp_profile` through a real `--mcp-config` registration in mock
mode, using a config passed for that one run.

**RX continuity limit.** Above 100,000 S/s, continuity is not guaranteed through this
server. A raw socket read kept 1,248,000 S/s for 6 s, so the board and link keep up, but
decoding in Python does not. At 250,000 S/s a 20 s stream passed in some runs and lost
samples in another. Separate captures of 65,536 samples at 250,000, 500,000 and 1,250,000
S/s were sometimes clean and sometimes held a gap, including at all three rates in a run
that shared the host with a test suite, so the script reports those as measurements and
does not pass or fail them. It does check
that deleting one sample from a real capture is detected.

Testing on the board found five defects in 0.2.0, now fixed:

- `rp_device_diagnostics` requested `/bazaar` URLs that stopped the running app, so a
  tool marked read-only was not.
- RX returned samples in the wrong order, with no error.
- Reconnecting immediately after disconnect failed.
- `board_model` read a devicetree file that OS 3.00 does not have.
- `loaded_image_label` reported failure for a file the SDR app deletes on purpose.

Extended testing and an independent review found three more defects, also fixed:

- Restoring sample order, added for this release, and decoding ran one sample at a time in
  Python, which limited sustained RX to about 130,000 S/s.
- `rp_rx_connect` reported success when another client owned the role, so the failure
  only appeared at the next read.
- A role refused by TCP reset could leave a session running at the board's default tuning
  while reporting the requested one. This came from code review, not the board: the
  server was not seen sending a reset, and the fix is tested on loopback.

## What remains unverified

- One board on one day is the whole sample. Other images may need
  `RP_RX_SAMPLE_ORDER=raw`, and other OS versions may lay out SSH evidence differently.
- SSH evidence is text from the board. It does not prove the active FPGA image or that
  the hashes match a trusted build.
- The FPGA tools that need Vivado were not run. No Vivado installation was available.
- RX was checked with the ADC's DC offset at low frequencies. RF input at HF, gain,
  noise floor and frequency accuracy were not measured.
- Whether diagnostics disturb a running TX or full-duplex session is unknown. Avoid
  calling them during such a session.
- Claude Code's registration instructions were checked against official docs;
  automated integration uses the official SDK's actual stdio client. No local
  Claude Code configuration was changed during development.
- SSH evidence on other boards and OS versions requires device acceptance. Missing evidence is reported
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
