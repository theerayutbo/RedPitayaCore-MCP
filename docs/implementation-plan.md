# RedPitayaCore-MCP Implementation Plan

**Goal:** Publish a functioning, safe MCP stdio server for Claude Code.
**Architecture:** Validated fixed-target configuration, source-verified RX
transport, bounded diagnostics and MCP tools; deterministic mock mode.
**Tech stack:** Python 3.11+, official MCP Python SDK 1.x, httpx, pytest, ruff,
mypy, bandit, pip-audit.
**Spec:** [design.md](design.md). Execution is already authorized by the user.

## Global constraints

- No mandatory site IP, secrets, TX sockets, register writes or FPGA loading.
- Mock default; live target explicit; read-only evidence clearly distinguished
  from operator-reported profile and requested RX settings.
- Lifecycle/RX side effects explicitly described and guarded.

## Review focus

Partial TCP reads/EOF; occupied socket roles without acknowledgement; HTTP
redirects/unbounded responses; malicious config/SSH input; cancellation cleanup
and concurrent MCP calls. Each is covered by transport or service tests.

## Tasks

- [x] Write and run failing configuration/protocol/safety tests; implement the
  matching configuration and verified RX protocol subset.
- [x] Write failing loopback/diagnostic tests; implement bounded TCP, HTTP and
  optional fixed-command SSH diagnostics with cleanup and timeouts.
- [x] Write failing mock/service and real stdio tests; implement MCP tools and
  lifecycle/RX guards. Confirm live TX/start remain unreachable.
- [x] Add README, provenance, security policy, license, locked dependencies and
  least-privilege GitHub Actions; review and run all checks.
- [ ] Create the public GitHub repository, push reviewed contents, inspect remote
  visibility/commit/CI and report hardware-validation limits.
