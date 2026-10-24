# Verified protocol subset

This document describes the only binary SDR protocol implemented by
RedPitayaCore-MCP. It is an independently written, RX-only subset verified from
Pavel Demin's `sdr-transceiver.c` at commit
[`71c3402a015df6136bdb751e8b1531388b8e4f92`](https://github.com/pavel-demin/red-pitaya-notes/blob/71c3402a015df6136bdb751e8b1531388b8e4f92/projects/sdr_transceiver/server/sdr-transceiver.c).
It is not SCPI.

## TCP roles

The SDR server listens on one TCP port, conventionally `1001`. Each new socket
begins with one unsigned 32-bit little-endian role word:

| Role word | Upstream meaning | This project |
| ---: | --- | --- |
| `0` | RX control | Implemented |
| `1` | RX data | Implemented |
| `2` | TX control | Absent |
| `3` | TX data | Absent |

The project has no encoder or connection path for roles 2 and 3.

Opening role 0 resets RX tuning and rate in the upstream server. Opening role 1
resets its RX FIFO. Closing the RX control connection resets tuning again. These
are device-side effects, so live RX requires both operator configuration
`RP_ALLOW_RX=true` and the MCP argument `acknowledge_side_effects=true`.

The upstream protocol sends no acknowledgement for role assignment or RX
configuration. A successful local socket write means only that the operating
system accepted the bytes. A reported `connected` state does not prove that the
remote server accepted the role, frequency, or sample rate.

## RX control words

After role 0, the client sends unsigned 32-bit little-endian control words.

Frequency is encoded directly:

```text
uint32_le(frequency_hz)
```

This project accepts integer frequencies from 1 through 60,000,000 Hz.

Sample rate is selected by setting bit 28 and placing a verified table index in
the low bits:

```text
uint32_le((1 << 28) | rate_index)
```

| Index | Sample rate (samples/s) |
| ---: | ---: |
| 0 | 20,000 |
| 1 | 50,000 |
| 2 | 100,000 |
| 3 | 250,000 |
| 4 | 500,000 |
| 5 | 1,250,000 |

No generic command encoder is exposed. In particular, this project does not
encode PTT, TX configuration, or unknown command classes.

## RX sample data

Role 1 returns interleaved little-endian IEEE-754 float32 pairs:

```text
I0, Q0, I1, Q1, ...
```

One complex sample is therefore eight bytes. `rp_rx_read` accepts at most 8,192
complex samples per call and returns at most 32 preview samples. The implementation
rejects partial pairs and non-finite values. Values are uncalibrated units; they
must not be interpreted as volts, watts, dBm, or calibrated field strength.

Mock mode produces deterministic tone data for client development. Mock values
are synthetic and do not validate RF hardware, FPGA logic, ADC scaling, network
timing, or compatibility with a deployed image.

## Passive probes and metadata

`rp_connection_probe` opens the selected configured service port and closes it
without sending application bytes. On the SDR port, an accepted probe briefly
occupies the server's accept loop while it waits for a role word. An open TCP port
proves only reachability at that instant; it does not identify the application,
FPGA image, device model, OS release, or source revision.

`rp_device_diagnostics` reads bounded HTTP metadata. `rp_system_diagnostics`
optionally runs a fixed set of read-only commands through the local OpenSSH
client. These observations are reported separately. Missing evidence remains
unknown, and an FPGA manager state does not identify the loaded image.

## Lifecycle boundary

The requested vendor start form is:

```text
GET /bazaar?start=sdr_transceiver?type=run
```

The exact Bazaar behavior was reviewed against Red Pitaya's command module at
commit [`0e46d64396a57752bf97315789d6cc6c9fbddad8`](https://github.com/RedPitaya/RedPitaya/blob/0e46d64396a57752bf97315789d6cc6c9fbddad8/Bazaar/nginx/ngx_ext_modules/ngx_http_rp_module/src/rp_bazaar_cmd.c),
and the target build's FPGA startup path against Pavel Demin's
[`fpga.sh` at `cbecbdd45735a04ad6ccb0094a242a1f820665b8`](https://github.com/pavel-demin/red-pitaya-notes/blob/cbecbdd45735a04ad6ccb0094a242a1f820665b8/rp_patchs/rp/sdr_transceiver/bazaar/fpga.sh).
Because starting this app may load FPGA state, live start is outside this
project's safety boundary. Live stop is also blocked because it changes global
controller ownership. `rp_app_lifecycle` is a dry-run planner by default and can
execute only in mock mode when `RP_ALLOW_LIFECYCLE=true`.

## Compatibility limits

The intended operator-reported target is STEMlab 125-14, Red Pitaya OS 3.00-57,
and `sdr_transceiver` 1.0-20260302 build `cbecbdd4`. The server does not claim to
measure or cryptographically verify those values. Device metadata, an open port,
or successful sample reads cannot establish the full target identity.

Hardware acceptance must begin with safe diagnostics and an operator-managed app
start. Confirm the device model, OS, installed app build, FPGA state, listening
service, and RX behavior independently before relying on live results.
