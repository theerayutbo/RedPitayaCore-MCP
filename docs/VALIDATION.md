# Validation and precise limits

Local v0.2.0 validation on 2026-10-25 used macOS ARM64, Python 3.12.1 and
MCP SDK 1.30.0. No Red Pitaya or licensed Vivado installation was used.

| Check | Result |
| --- | --- |
| Pytest | 129 passed, including real MCP stdio clients and the 25-tool surface |
| Network fixtures | Loopback TCP and HTTP only; no Red Pitaya contacted |
| Ruff lint and formatting | Passed |
| Strict mypy | Passed for all eleven source files |
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

## What remains unverified

- Real STEMlab 125-14 operation on official OS 3.00-57 and the installed app
  binary were not tested. Source compatibility is documented in
  [PROVENANCE.md](PROVENANCE.md); it does not identify a device executable.
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
