# STEMlab 125-14 with `sdr_transceiver`: field report, issues found and suggested fixes

Report for Red Pitaya support. Several items concern the `sdr_transceiver` application from
pavel-demin/red-pitaya-notes (MIT), which Red Pitaya distributes through its application
marketplace. Where an issue lies in that project rather than in the board, OS or web stack, the
**Owner** column says so, so that it can be forwarded upstream.

We use the board as the RF front end of a narrowband HF data modem: 50 kS/s complex baseband,
carriers between 2 and 30 MHz, burst-mode transmission, and a host PC that streams over the
`sdr_transceiver` TCP protocol. All tests were run on cables and attenuators, with no antenna.
Application-specific details have been left out.

Evidence labels:

| Label | Meaning |
|---|---|
| **measured** | measured on the board described below |
| **source** | read from the server or FPGA source, not measured |
| **inferred** | best explanation, not proven |

## 1. Environment

| Item | Value |
|---|---|
| Board | STEMlab 125-14 v1.0 |
| OS | Red Pitaya OS 3.00-57 (Ubuntu 24.04.4) |
| Application | `sdr_transceiver` 1.0-20260302, revision `cbecbdd4` |
| Server source referenced | `projects/sdr_transceiver/server/sdr-transceiver.c` at that revision, cited as `C:<line>` |
| Host | macOS, Python client written from the server's behaviour, direct 1000BASE-T link |
| RF set-ups | (a) OUT1 → 30 dB attenuator → IN1; (b) OUT1 → IN1 direct; (c) cross-cabled OUT1 → IN2 and OUT2 → IN1 (two stations on one board) |
| Test period | 2026-11-02 to 2026-11-09 |

## 2. Summary

| ID | Issue | Owner | Impact | Evidence |
|---|---|---|---|---|
| RP-1 | First TX `recv` of a connection pushes stale stack bytes to the DAC: a pulse 15-20 times a 0.1 FS tone at every TX start | `sdr_transceiver` server | RF: out-of-limit pulse at every start | measured + source |
| RP-2 | Closing TX control retunes TX to 600 kHz / 100 kS/s while queued samples still play | `sdr_transceiver` server | RF: emission off the commanded carrier for 0.07-0.30 s | measured + source |
| RP-3 | TX FIFO underrun inserts zeros silently (`ALWAYS_VALID`), with no counter or flag | `sdr_transceiver` FPGA + server | Data integrity: damaged bursts nobody can detect | measured + source |
| RP-4 | RX FIFO overflow: the server resets the FIFO and drops samples silently | `sdr_transceiver` server | Data integrity: 4.7k-55k samples lost with no sign in the stream | measured + source |
| RP-5 | RX (and, after a rebuild, TX) sample order depends on glibc `memcpy` | `sdr_transceiver` server + build / OS image (glibc) | Spurs unless the client reorders | measured (known upstream, issue #1205) |
| RP-6 | PTT has no watchdog and does not gate OUT | `sdr_transceiver` server / FPGA design / documentation | Safety: PTT stays high if the client stalls or the link drops | measured |
| RP-7 | Single-threaded accept loop blocks on the role word of each new connection | `sdr_transceiver` server | Any idle connection blocks every other client | source + measured |
| RP-8 | No acknowledgement: a refused role is just closed, and an immediate reconnect is dropped | `sdr_transceiver` server | Robustness | measured |
| RP-9 | Opening or closing a role resets settings (600 kHz / 100 kS/s) | `sdr_transceiver` server / documentation | Surprising side effects | source |
| RP-10 | Frequency word in whole Hz, NCO in 0.1164 Hz steps: a fractional-Hz carrier can be off by up to ~0.56 Hz | `sdr_transceiver` protocol | Feature request (workaround below) | measured + source |
| RP-11 | `GET /bazaar?help` and `?apps` stop a running application | Red Pitaya web stack | Monitoring tools kill the app | measured |
| RP-12 | OUT → IN leakage on the same channel is about −55 dB at 7.1 MHz | board (documentation) | A sensitive receiver decodes its own transmissions | measured |
| RP-13 | TX level errors below ~0.01 FS; H3 at low and mid amplitudes; input termination note | board / documentation | Questions for the hardware team | measured |
| H-1 | macOS can move the 169.254/16 route to Wi-Fi: the board "disappears" | host OS (documentation) | Support burden | measured |
| H-2 | Client host stalls versus TX lead and RX buffering: measured limits | client guidance | Data integrity | measured |

## 3. Issues in detail

### RP-1. First TX `recv` pushes stale stack bytes to the DAC

**Symptom (measured).** Every burst began with a pulse shorter than 1.3 ms, about one block
before the signal, at 15-20 times the level of a 0.1 FS tone: RX level 0.0147-0.0193 against
0.00098 for the tone through 30 dB. It exceeded every amplitude limit enforced on the client
side. Sending only whole 16384-byte blocks, sending zero preroll and setting `TCP_NODELAY` did
not remove it. Note that even I and Q both at full scale would only give about 14 times a
0.1 FS tone. We cannot explain the rest of the ratio, so treat the exact figure with care; the
pulse itself was seen at every start.

**Reproduction (protocol level).**
1. Connect to TCP 1001 and send the role word `2` (u32 LE, TX control). Then send the carrier
   word `0<<28 | 7100000` and the rate word `1<<28 | 1` (50 kS/s).
2. Open a second connection and send the role word `3` (TX data).
3. Stream 16384-byte blocks of complex float32 (I, Q) carrying a 0.1 FS tone.
4. Receive on IN1 (OUT1 cabled to IN1 through an attenuator) at 50 kS/s, and look at `|x|`
   before the tone starts. Do not use a window that starts after the onset: it hides the pulse.

**Cause (source).** `tx_data_handler` blocks in `recv(sock, buffer, 16384, 0)` without
`MSG_WAITALL` (C:339), then copies all 16384 bytes into the FIFO (C:340). The server is
already waiting when the client's first segment arrives, so that `recv` returns short. The
rest of the block is stale stack, which turns into large floats (inferred). The pulse sits at
the end of the first block (measured).

**Suggested fix.** Loop until a whole block has been received, or use `MSG_WAITALL`, before
copying to the FIFO.

**Client workaround (measured, removes it every time).** The accept loop is single-threaded
and blocks reading the role word of each new connection (C:138), see RP-7. A client can use
that:

1. Open an idle TCP connection and send nothing.
2. After 50 ms, open the TX data connection and send its role word plus two zero blocks at
   once. They wait in the board's kernel.
3. After 50 ms, close the idle connection. The server then accepts the TX data connection,
   and its first `recv` finds whole blocks.

After this change the peak before the tone was 0.0013-0.0035 times the tone level (the noise
floor) at 20-250 kS/s, against 15-20 times before. It held over 12 start/stop
cycles in a 2 h soak. The workaround depends on LAN timing and briefly blocks other clients,
so a server fix is preferable.

### RP-2. Closing TX control retunes TX while queued samples still play

**Symptom (measured).** We transmitted at 7.1 MHz and then closed the TX control connection,
or the client process was killed. The carrier stopped at 7.1 MHz within 15 ms, but the queued
signal reappeared near 600 kHz:

| TX rate | Duration near 600 kHz | Offset of a +5 kHz baseband tone |
|---:|---:|---:|
| 20 kS/s | 0.131 s | +25 kHz |
| 50 kS/s | 0.115 s (0.108-0.132 s on a normal stop) | +10 kHz |
| 250 kS/s | 0.300 s | +2 kHz |

The offset scales as `offset × 100k / rate`, so the queued samples play at the reset rate of
100 kS/s. On a normal stop the level near 600 kHz was about 24,000 times the noise floor.

**Cause (source).** When TX control closes, the handler drops PTT and immediately sets
`tx_freq` to 600 kHz and the rate to 100 kS/s (C:308-314). Meanwhile the data thread and the
FIFO keep playing the queue (C:322-347).

**Reproduction.** As for RP-1, with a baseband tone offset of +5 kHz. While samples are still
queued, close the TX control socket, or kill the client. Receive on IN1 with RX tuned to
600 kHz.

**Why it matters.** It is an emission off the commanded frequency, and in our case also off
the HF band. A client cannot prevent it if its process is killed or power fails.

**Suggested fix.** On TX control close, mute the DAC path or flush the TX FIFO before
retuning, or do not retune at all.

**Client workaround (measured).** Keep TX control and PTT open until the queue has played:
stop feeding, send three zero blocks, then wait for the modelled DAC end plus 6144 samples.
After this, 0 ms of energy appeared near 600 kHz (6/6 runs at 50 kS/s, 3/3 at 100k and 250k).
The cost is a slower stop: 0.44 s at 50 kS/s. A killed process still leaves the queue to play
at 600 kHz, about 0.07-0.15 s at 50 kS/s depending on the client's lead.

### RP-3. TX FIFO underruns insert zeros with no indication

**Symptom (measured).** When the TX FIFO runs empty, the DAC chain receives zeros. The output
shows a notch, then the signal continues shifted by a whole number of samples, and no flag or
counter records the event. Two independent causes were measured:

1. **Server side, at high rates.** At 1.25 MS/s the server thread refills the FIFO only when
   2048 samples (1.64 ms) remain, and polls with `usleep(500)`. During streaming it sometimes
   wakes 2.1-2.7 ms late, about once per second. `chrt -f -p 50` on the server reduced this to
   one event in 200 s with one channel. It does not help with both TX channels at 1.25 MS/s.
   The full data is in a companion report, "TX FIFO underruns at 1250 kS/s silently insert
   zeros", available on request.
2. **Client side, at any rate.** If the host falls behind by more than what it keeps queued
   ahead of the DAC, the FIFO empties mid-burst. We measured this deliberately: we froze the
   sending process for a fixed time in the middle of a burst. The output then failed once the
   freeze exceeded about the client's lead (table in H-2). In a controlled campaign under CPU
   load (432 transfers), every failure had such an underrun at its sender: 9 of 9 failures out
   of 144 transfers with a 164 ms lead, none at longer leads.

**Source.** `tx.tcl`: `axis_fifo` with `WRITE_DEPTH 8192` (4096 complex samples) and
`ALWAYS_VALID TRUE`. `cores/axis_fifo.v`: when empty, `tdata` = 0 and `tvalid` stays high.

**Suggested fixes.**
- A sticky TX underrun counter in the status word that the client can read. Today a client
  can only estimate underruns from its own timing, as we do.
- Refill earlier and in smaller pieces, for example whenever 1024 samples are free.
- A deeper TX FIFO if block RAM allows.
- Real-time scheduling of the server threads, or documentation of `chrt` for high rates.

### RP-4. RX FIFO overflow drops samples silently

**Symptom (measured).** If the client stops reading for longer than the TCP buffers can absorb
(about 0.9 s at 50 kS/s on our link), samples vanish from the RX stream. The stream carries no
counter or timestamp, so nothing shows where they went. We froze the receiving process for a
fixed time in the middle of a received burst:

| Freeze | Received bursts intact | Samples lost (independent estimate) |
|---|---|---|
| 0-0.75 s | 30/30 | 0 (±50, the noise of the estimate) |
| 1.0 s | 4/9 | 0 when intact, otherwise 4,688-7,729 (at the buffer limit) |
| 2.0 s | 0/8 | 44,908-54,924 |

A 2 s freeze lost about 1.08 s of samples, so roughly 0.9 s was buffered and the rest dropped.
Downstream, a decoder without its own integrity check then delivers wrong data: about 1-9 kB
of garbage per event in our tests. With the client-side detector below, the frame was cut
short instead (720 B, of which 144-288 B were wrong).

**Cause (source).** The RX handler resets the FIFO when `rx_cntr >= 8192` (C:231-235). The
samples are lost with no sign in the stream.

**Suggested fixes.**
- Count dropped samples or overflow events in a status word, or add a sequence number or
  timestamp to each RX block. Either would let clients detect the gap exactly.
- Document the behaviour and the buffer it allows.

**Client workaround (measured).** We infer the loss from arrival times. We take only points
where the socket has just been drained (FIONREAD = 0) and compute
`offset = arrival_time − bytes_received / 8 / fs`. A loss raises the lower envelope of the
offset for good; a stall that the buffers absorbed raises it only until the backlog is read.
A lasting rise of more than 25 ms for 0.5 s over at least 3 drained points is declared a loss.
The detector found 8 of 8 real losses, with sample-count estimates within 1% of an independent
measurement. It raised no false alarm in about 590 transfers without a loss (432 of them in the
controlled campaign of H-2, and 24 under very heavy host load, load average up to 490). It confirms about 0.5 s after the loss, so a server-side counter would still be much
better.

### RP-5. Sample order depends on glibc `memcpy`

**Measured.** RX samples arrive in a fixed wrong order inside every 16384-byte block, counted
from the first byte of the connection. A pure tone then shows spurs at `f + k·fs/8`, about
−11 dBc. This is the known effect of the ARMv7 glibc `memcpy` reading the pop-on-read FIFO
window out of order (red-pitaya-notes issue #1205, "Debian sdr applications issue", where the
maintainer describes the same `memcpy` behaviour and notes that musl on Alpine reads in order). Our client applies an inverse permutation;
after it the worst spur was −64.3 dBc at 600 kHz and −83.2 dBc or better elsewhere.

We rebuilt the same source on the board with the Ubuntu gcc, still with glibc `memcpy`. Both
RX and TX then came out in a different order, and a test tone had a spur at −9.9 dBc. So the
order changes with the build and with buffer alignment. Replacing both copies with in-order
64-bit loops gave correct order in both directions, with no client reordering, and TX spurs of
−55 to −61 dBc.

**Suggested fix.** Use explicit in-order word loops for all FIFO reads and writes, as
proposed in #1205.

### RP-6. PTT has no watchdog and does not gate OUT

**Measured,** by reading the PTT bit through `/opt/redpitaya/bin/monitor`:

| Event | PTT |
|---|---|
| Tone sent without a PTT command | low, and OUT carries the signal |
| Client stops feeding, sockets still open | stays high |
| Only TX data closed | stays high |
| TX control closed | low |

If the network link is lost, no FIN reaches the board. The server then waits in `recv`, and
PTT probably stays high until TCP times out (source, not measured on the board). During a
host-side network outage in our tests, the client could not deliver its PTT-low command. We did
not observe the PTT pin during that outage.

**Why it matters.** A PTT line that keys an amplifier can stay keyed indefinitely after a
client crash, a host sleep or a cable fault.

**Suggested fixes.**
- A server-side TX watchdog: if no TX data arrives for a configurable time while PTT is high,
  drop PTT and mute the DAC.
- TCP keepalive on the control sockets.
- Documentation stating that PTT is a GPIO only and does not gate OUT.

### RP-7. Single-threaded accept loop blocks on each new connection's role word

**Source.** After `accept`, the main loop reads the 4-byte role word with `MSG_WAITALL`
(C:138) before accepting anything else.

**Measured.** An idle connection that sends nothing holds the server, and no other client can
connect until it closes. We use this deliberately in the RP-1 workaround. It also means any
stray connection, such as a port scanner or a crashed client, blocks all control.

**Suggested fix.** Read the role word in the per-connection thread, or with a short timeout.

### RP-8. No acknowledgement; an immediate reconnect is dropped

**Measured.**
- If a role is already taken, the server just closes the new socket and sends no error. A
  client that does not check sees an apparently successful connection. We check about 50 ms
  later for EOF on both sockets.
- Reconnecting 0 ms after a close was dropped. After 50 ms it worked. We use a 200 ms guard.

**Suggested fix.** Reply to the role word with a status byte, or at least document the
behaviour.

### RP-9. Opening or closing roles resets settings

**Source.**
- Opening or closing RX control resets RX to 600 kHz at 100 kS/s.
- Opening RX data resets the RX FIFO.
- Opening TX control drops PTT and resets TX.

Clients must therefore resend every setting on each connection.

**Suggested fix.** Document it in the protocol description.

### RP-10. Frequency resolution: whole Hz, then 0.1164 Hz NCO steps

**Source.** The carrier word holds whole Hz (28-bit field). The server converts it to a 30-bit
phase increment of the 125 MHz clock, `floor(Hz / 125e6 × 2^30 + 0.5)`. So the NCO moves in
steps of 0.1164 Hz. Against the whole-Hz request the rounding error is at most half a step
(0.058 Hz). Against a wanted fractional-Hz carrier, such as 8,225,000.125 Hz, the error can reach
about 0.56 Hz (0.5 Hz from the whole-Hz field plus half an NCO step).

**Measured.** At 40 random carriers from 2 to 30 MHz, the received offset matched this model
to within 20 µHz. Without correction, the offset between a TX and an RX carrier requested a few Hz
apart (both ends rounded) differed from the requested offset by up to 0.87 Hz.

**Feature request.** A control word that carries the phase increment directly, or a fractional
Hz field.

**Client workaround (measured).** Apply the residual as a phase-continuous baseband rotation:
before the DAC for TX, after the ADC for RX. The error was then ≤ 20 µHz relative to the board
clock at all 40 frequencies, and 400 of 400 modem frames decoded. Rotate only non-zero
samples: a complex multiply turns +0.0 into −0.0, and a client that treats "any non-zero byte"
as signal will then see its silence as signal.

### RP-11. `/bazaar` query endpoints stop a running application

**Measured.** `GET /bazaar?help` and `GET /bazaar?apps` stop the running application within
0.5 s. `info.json` is safe, but it answers even when the application is stopped, so it cannot
confirm that the application is running. We now check by connecting to TCP 1001 instead.

**Suggested fix.** Make query endpoints free of side effects, and add a status endpoint that
reports whether an application is running.

### RP-12. Same-channel OUT → IN leakage

**Measured** at 7.1 MHz with a 0.05 FS tone, cross-cabled (OUT1 → IN2, OUT2 → IN1):

| Path | Relative level |
|---|---|
| TX1 → IN1 (own channel, through the board) | −56 dB relative to TX1 → IN2 via the cable |
| TX2 → IN2 (own channel) | −55 dB |
| Channel isolation, direct cables, 0.1 FS | TX1 → RX2 56.6 dB, TX2 → RX1 54.7 dB |

**Why it matters.** A narrowband receiver with high processing gain can decode its own
transmissions through this path. In our use this caused a measurable failure, which we now
handle in software. Applications that use both channels, or listen while transmitting, should
know this figure.

**Request.** Publish the OUT → IN and channel-to-channel isolation versus frequency in the
board documentation.

### RP-13. Hardware observations (questions for the hardware team)

The level table was measured with coherent detection: the tone is mixed to DC and averaged
over a fixed window, because FFT peak levels shift with scalloping. The harmonic and dBc
figures come from windowed FFTs (Blackman-Harris for the direct-cable values, FFT peak for the
attenuator values) and are limited by our measurement floor.

**TX level at low amplitude.** Direct cable OUT1 → IN1, level relative to the same tone at
0.1 FS:

| Amplitude (FS) | 1 MHz | 7.1 MHz | 14.2 MHz |
|---:|---:|---:|---:|
| 0.000158 (~1 LSB) | −58.9 dB | −53.9 dB | −58.9 dB |
| 0.000316 | −3.5 dB | +1.1 dB | +4.4 dB |
| 0.00063 | −3.2 dB | −2.4 dB | −0.8 dB |
| 0.002 | −1.2 dB | −1.6 dB | −1.9 dB |
| 0.005 | −0.3 dB | −0.4 dB | −0.5 dB |
| 0.01-0.05 | within ±0.09 dB | within ±0.09 dB | within ±0.09 dB |
| 0.25-1.0 | ±0.01 dB | ±0.01 dB | ≥ −0.05 dB (no compression) |

Below about 0.01 FS the output level falls, and a tone at about 1 DAC LSB almost disappears.
That is consistent with rounding in the DAC path (inferred). Through a 30 dB attenuator the
RX side added its own errors when only a few ADC LSBs remained, up to −15.2 dB at 0.002 FS and
14.2 MHz.

**Third harmonic (direct cable, 7.105 MHz).**
- H3 was −30, −35 and −36 dBc at 0.05, 0.1 and 0.2 FS.
- It was −54, −61 and −64 dBc at 0.3, 0.5 and 0.7 FS, and −52 dBc at 0.9 FS.
- Through the attenuator it was −58 to −67 dBc.
- H2 was −53 dBc at 0.1 FS and −62 to −67 dBc above that.
- Carrier leak and image were ≤ −99 dBc, limited by our measurement floor.

The H3 is repeatable to 0.1 dB and follows the harmonic frequency when the tone moves. It does
not grow with amplitude, so we cannot tell whether it arises in the DAC, the ADC or the cable.
It needs a spectrum analyser.

**Set-up note (documentation): input with an unterminated cable.** With a coaxial cable of
unrecorded length from OUT to a high-impedance IN, the received level of a 0.05 FS tone rose by
21.5 dB from 2 MHz to about 23.7 MHz, then fell by 30 MHz. We attribute this to the cable, not
to the board (inferred). Users who measure levels across HF should terminate the input
in 50 Ω or use an attenuator. A note in the documentation would help.

## 4. Host-side notes (not board defects; suggested for documentation or FAQ)

### H-1. macOS and link-local addressing

**Measured.** With the board on a direct cable using link-local addressing (169.254/16) and
Wi-Fi also active, macOS moved the 169.254/16 route to the Wi-Fi interface during a long
test. The board then looked "down", although it never lost link: board-side
`carrier_changes` stayed at 1, it did not reboot, and the kernel log showed only the link-up
at boot.

`route add -host <board> -interface <eth>` makes it worse on macOS. It creates a permanent ARP
entry that maps the board's IP to the Mac's own MAC, so packets never leave
(`No route to host`).

What worked:

```sh
sudo route -n delete -net 169.254.0.0/16
sudo route -n add -net 169.254.0.0/16 -interface <eth>
```

A static address on a private subnet for the direct link is the permanent fix. Recommending a
static address for direct-cable use would save users time.

### H-2. How long a client may stall

Both directions depend on the client keeping up, and the board reports neither failure (RP-3,
RP-4). Our measurements at 50 kS/s:

**TX: the stall tolerance equals the client's lead.** Here "lead" is what the client keeps
written to the TX data socket ahead of its own estimate of when the DAC runs out (it paces on
that estimate). The board FIFO holds some more, but the measured tolerance matched the lead
alone. The sending process was frozen in the middle of a burst, two runs per point:

| Lead | Survived | Failed |
|---|---|---|
| 4 blocks (164 ms) | 0.1 s | 0.2 s (1 of 2), 0.3 s and longer (all) |
| 8 blocks (328 ms) | 0.3 s | 0.4 s and longer |
| 16 blocks (655 ms) | 0.6 s | 0.8 s |

Each block of lead adds 41 ms of TX latency. Under synthetic CPU load, with 32 busy processes
on 8 cores, 6 interleaved cycles and 144 transfers per lead:

| Lead | Failed transfers | 95% CI |
|---|---|---|
| 164 ms | 9/144 (6.2%) | 3.3-11.5% |
| 328 ms | 0/144 | 0-2.6% |
| 655 ms | 0/144 | 0-2.6% |

Every failure had a TX underrun at its sender, and no transfer failed without one. The host
stalls behind them were 0.17-0.21 s (in the 6 transfers where it could be estimated).

**RX:** about 0.9 s of buffering at 50 kS/s on our link (RP-4).

**Also measured:**
- Pure CPU load did not stall a sleeping thread for long. Even at a load average of 142, the
  worst wake-up delay of a separate process was 10 ms.
- Long stalls came from other applications spiking and, we infer, from contention inside one
  process (Python GIL).
- Under very heavy load (load average up to 490), demodulation itself could not keep real
  time.

Documenting these limits, or adding the counters suggested in RP-3 and RP-4, would let users
size their buffers.

## 5. Suggested changes, in priority order

1. **RP-2:** mute or flush TX before the retune on control close. This is an off-frequency
   emission.
2. **RP-6:** add a server-side TX/PTT watchdog, and document that PTT does not gate OUT.
3. **RP-1:** read a whole block before the FIFO copy.
4. **RP-3 / RP-4:** add underrun and overflow counters in the status word, or sequence numbers
   on RX blocks.
5. **RP-5:** use in-order FIFO copies (issue #1205).
6. **RP-7 / RP-8:** read the role word off the accept thread, and acknowledge it.
7. **RP-11:** make `/bazaar` queries free of side effects, and add a status endpoint.
8. **RP-10:** add a phase-increment or fractional frequency word.
9. **Documentation:**
   - RP-9 (resets on connect and close)
   - RP-12 (isolation figures)
   - RP-13 (input termination)
   - H-1 (static IP for direct links)
   - H-2 (client stall budgets)

## 6. Not verified

- Absolute RF levels in volts or dBm. No calibrated instrument was used, so all levels are
  relative.
- The RP-1 pulse measured directly at the OUT connector. It was measured through 30 dB and IN1,
  as a ratio to the tone.
- The PTT pin at the connector. It was read from the register only.
- PTT during a real network-link loss on the board side (RP-6 reasoning is from source).
- Other boards and OS versions. Everything here is from one STEMlab 125-14.
- The server patches mentioned (exact-block TX read, in-order copies) as a permanent
  installation. They were tested only as test builds.

## 7. Methods (for reproduction)

- **Level:** mix the known tone frequency to DC and average over a fixed long window
  (coherent). Do not use FFT peaks: scalloping shifts them by up to 0.7 dB.
- **Sample loss and steps:** mix a tone to DC, average over 64 samples, and count phase steps
  that persist above 0.1 rad. As a control, delete one sample to prove the detector sees it
  (0.611 rad).
- **Silence after stop:** listen at every frequency the signal can move to, here near 600 kHz,
  not only at the carrier.
- **Underrun on the board:** poll `tx_cntr` (status page `0x41000000`, offset 2) read-only.
- **Freezing a process** (SIGSTOP/SIGCONT for a fixed time) gives stall tests that can be
  reproduced exactly, unlike CPU load.

We can provide sanitised measurement scripts and raw results on request.
