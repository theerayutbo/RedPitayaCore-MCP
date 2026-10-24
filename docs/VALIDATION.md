# Validation and precise limits

Local validation on 2026-10-24 used macOS ARM64, Python 3.12.1 and MCP SDK 1.30.0.

| Check | Result |
| --- | --- |
| Pytest | 48 passed, including a real MCP stdio client |
| Network fixtures | Loopback TCP and HTTP only; no Red Pitaya contacted |
| Ruff lint and formatting | Passed |
| Strict mypy | Passed for all seven source files |
| Bandit | No findings in source |
| pip-audit | 70 dependencies audited; zero known vulnerabilities reported |
| Wheel and source distribution | Built successfully |
| Repository review | No credentials, site IP, captures or private source copied |

The new local project itself is not published on PyPI and was skipped by the
dependency vulnerability database. Its source is covered by review and static
analysis; an audit with no known findings is not a security guarantee.

An independent review identified and verified fixes for cancellation during
disconnect, compressed HTTP response expansion, concurrent diagnostic process
growth, and ambiguous TX state reporting. Regression tests cover these cases.

GitHub Actions repeats checks on Python 3.11, 3.12 and 3.13, with a separate
dependency audit. Check the repository's Actions page for the current result.
Actions use pinned commit references and read-only repository permissions.

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
- No TX path exists in this MCP. Hardware TX state remains unknown; the server
  does not stop another client, enforce an RF interlock or force outputs off.
- Samples are uncalibrated application units; no voltage, power, ADC clipping,
  RF performance or regulatory compliance claim is made.
