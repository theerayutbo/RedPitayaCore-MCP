# Official SCPI instrument mode (experimental)

Separate entry point within RedPitayaCore-MCP; existing SDR/FPGA server unchanged.
Supports STEMlab 125-14, operator-started official scope/SCPI application on OS
3.00-57. No automatic application lifecycle, FPGA switching or arbitrary SCPI.
Never run the SDR and SCPI services against the same board simultaneously.
No board acceptance has been performed on this implementation.

## Tools
- rp_acquisition_status: local configuration, no board contact.
- rp_frequency_sweep_plan: offline frequency/decimation/burst plan.
- rp_adc_capture: synchronized IN1/IN2 from common trigger, no generator commands.
- rp_frequency_sweep: stepped sine, one finite OUT1 burst per frequency, AWG common trigger.
  Internal generator config/disable replaces a persistent standalone generator tool.
  Captures 4096 post-trigger samples per channel at 125 MHz / decimation.
  Raw count or board-reported volt units; board calibration not independently verified.
  No claim of exact hardware timestamps, phase calibration or steady-state validity.

## Mock registration (local Mac)
```sh
claude mcp add --scope project --transport stdio -e RP_MODE=mock redpitaya-instrument -- /absolute/path/to/RedPitayaCore-MCP/.venv/bin/python -m redpitayacore_mcp.instrument_server
```
Call rp_frequency_sweep_plan with frequencies_hz [100000, 1000000].
Then call rp_frequency_sweep in mock. NPZ contains in1, in2, metadata (JSON string).
Read with numpy.load(path, allow_pickle=False). Default captures directory is local,
set RP_INSTRUMENT_OUTPUT_DIR to a private directory. No data uploaded by service.

## Live acceptance (operator-supervised only)
1. Stop existing SDR client and application manually. Confirm official SCPI app is
   running and board model/OS, exclusive ownership and no other client.
2. Qualify physical input jumpers, limits, load impedance, grounding and protection;
   start with an appropriately attenuated cabled loopback, not an antenna or an
   uncharacterized coil. ADC gain command is not a replacement for physical jumpers.
3. Set operator-local RP_MODE=live, RP_HOST, RP_ALLOW_SCPI=true, RP_ALLOW_RX=true,
   RP_SCPI_PORT (default 5000). For finite bursts also RP_ALLOW_TX=true,
   RP_TX_MIN_HZ/RP_TX_MAX_HZ, RP_SCPI_MAX_VPK (default 0.05 V peak, hard ceiling 0.1 V),
   RP_TX_MAX_SECONDS. Voltage setting is not the actual loaded voltage or current.
   Operator permission must be confirmed per call. No automatic output on startup.
4. Call rp_adc_capture only after confirming input configuration and scope ownership.
   Validate data units/length, sample alignment and clipping against reference signals.
5. Call rp_frequency_sweep only after verifying the connected load and explicit RF
   acknowledgement. Start with a single qualified frequency; confirm actual output
   stops, burst length, channel alignment and phase on a scope. Repeat across bands.
6. Keep probe and specimen stationary initially; characterize settling, select a
   valid subwindow and calibrate current/reference gain and relative channel phase.
   Host command/poll bracket is not exposure or acquisition time: do not join it
   to moving camera poses as a precise timestamp. NPZ metadata deliberately contains
   no fabricated hardware timestamp or calibration ID.
7. Stop on error and inspect board manually. Cleanup sends output disable and
   acquisition stop, but network/host faults can prevent cleanup; finite burst
   limits are programmed and read back, not a verified hardware fail-safe.
   Do not use with unattended hardware. A failed sweep may leave completed NPZ
   files locally; no successful complete result is returned on failure.

## References
Official acquisition commands: https://redpitaya.readthedocs.io/en/latest/appsFeatures/remoteControl/command_list/commands-acq.html
Official generation commands: https://redpitaya.readthedocs.io/en/latest/appsFeatures/remoteControl/command_list/commands-gen.html
Common AWG trigger example: https://redpitaya.readthedocs.io/en/latest/appsFeatures/examples/acquisition_generation/acq_genRF-1-synced.html
