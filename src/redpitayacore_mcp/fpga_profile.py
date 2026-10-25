"""Pinned FPGA interface facts and non-executing hardware/debug plans."""

from __future__ import annotations

import re
from typing import Any

from .toolchain import tcl_quote

REVISION = "cbecbdd45735a04ad6ccb0094a242a1f820665b8"
SOURCE = (
    f"https://github.com/pavel-demin/red-pitaya-notes/blob/{REVISION}/"
    "projects/sdr_transceiver/server/sdr-transceiver.c"
)
# Facts from the pinned server's typed pointers; these are not a generic MMIO API.
_REGISTERS = (
    ("rx_reset", 0x40000000, 8, "write", "RX FIFO reset; edge-sensitive"),
    ("tx_reset", 0x40000001, 8, "write", "TX FIFO reset; edge-sensitive"),
    ("gpio_ptt", 0x40000002, 8, "write", "Bit 0 can key external RF hardware"),
    ("rx_phase_increment", 0x40000004, 32, "write", "Changes RX digital tuning"),
    ("rx_sync", 0x40000008, 8, "write", "Changes RX synchronization"),
    ("rx_rate_divisor", 0x4000000A, 16, "write", "Changes RX decimation"),
    ("tx_phase_increment", 0x4000000C, 32, "write", "Changes TX digital tuning"),
    ("tx_sync", 0x40000010, 8, "write", "Changes TX synchronization"),
    ("tx_rate_divisor", 0x40000012, 16, "write", "Changes TX interpolation/rate"),
    ("rx_fifo_count", 0x41000000, 16, "read", "Status count; no live reader implemented"),
    ("tx_fifo_count", 0x41000002, 16, "read", "Status count; no live reader implemented"),
)


def register_map() -> dict[str, Any]:
    return {
        "profile": "sdr_transceiver-instance-1",
        "source_revision": REVISION,
        "source_url": SOURCE,
        "live_access_supported": False,
        "image_identity_verified": False,
        "registers": [
            {
                "name": name,
                "address": f"0x{address:08X}",
                "width_bits": width,
                "upstream_access": access,
                "side_effects": effects,
            }
            for name, address, width, access, effects in _REGISTERS
        ],
        "forbidden_fifo_aperture": "0x42000000",
        "note": "Typed access observed in source does not establish safe readback semantics. "
        "Profile is not inferred from a TCP port or verified against the live image.",
    }


def register_plan(
    name: str, operation: str, value: int | None = None, expected_value: int | None = None
) -> dict[str, Any]:
    entry = next((entry for entry in register_map()["registers"] if entry["name"] == name), None)
    if entry is None or operation not in {"read", "write"}:
        raise ValueError("Use a named register from the pinned profile and read/write operation")
    if entry["upstream_access"] != operation:
        raise ValueError("This access direction is not established by the pinned server source")
    for number in (value, expected_value):
        if number is not None and (
            type(number) is not int or not 0 <= number < (1 << entry["width_bits"])
        ):
            raise ValueError("Register value must fit the exact unsigned access width")
    if operation == "write" and value is None:
        raise ValueError("A register write plan requires a value")
    if operation == "read" and value is not None:
        raise ValueError("A read plan must not contain a write value")
    return {
        **entry,
        "operation": operation,
        "value": value,
        "expected_value": expected_value,
        "executed": False,
        "execution_supported": False,
        "blockers": [
            "Active image identity and register readback semantics are unverified",
            "No live MMIO adapter or hardware mutation authorization is configured",
        ],
        "note": "This is a source-based plan, not an observed register value or approved write",
    }


def hardware_plan(operation: str) -> dict[str, Any]:
    common = [
        "Board profile and active image identity require independent verification",
        "Exclusive maintenance ownership and a tested recovery path are required",
        "Live hardware adapter and per-operation human authorization are absent",
    ]
    specific = {
        "load_bitstream": [
            "Approved bitstream and DTBO hashes must match the exact board",
            "Stop conflicting apps and verify a board-local recovery image",
            "Installed-file hashes alone do not identify the active FPGA image",
        ],
        "register_write": [
            "Use named per-image fields, access widths, allowed bit masks and ranges",
            "Verify expected state/readback only where the design supports it",
            "FIFO/reset/PTT operations need separate reviewed state transitions",
        ],
        "tx_burst": [
            "An approved exact-frame server is required; stock TX has a short-read bug",
            "A board-local watchdog and independent physical TX inhibit are required",
            "Verify dummy load, attenuation, external PA/PTT wiring and power limits",
            "Bound RF duration/frames; network disconnect is not an RF-off guarantee",
        ],
    }
    if operation not in specific:
        raise ValueError("Operation must be load_bitstream, register_write or tx_burst")
    return {
        "operation": operation,
        "executed": False,
        "execution_supported": False,
        "hardware_tx_state": "unknown_not_read",
        "blockers": common + specific[operation],
        "source_url": SOURCE,
        "note": "No environment switch enables these live operations in this release",
    }


def ila_plan(clock_net: str, probes: list[list[str]], depth: int = 1024) -> dict[str, Any]:
    if (
        not 1 <= len(probes) <= 8
        or any(not probe for probe in probes)
        or sum(len(probe) for probe in probes) > 256
    ):
        raise ValueError(
            "Provide 1 to 8 probes, each nonempty, totalling at most 256 exact bit nets"
        )
    names = [clock_net, *(net for probe in probes for net in probe)]
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./\[\]-]{0,255}", net) for net in names):
        raise ValueError("Use exact clock/probe net names, without Tcl expressions or wildcards")
    if type(depth) is not int or depth not in {1024, 2048, 4096, 8192}:
        raise ValueError("ILA depth must be 1024, 2048, 4096 or 8192")

    def selection(net: str) -> str:
        return f"[get_nets -hier -regexp {tcl_quote('^' + re.escape(net) + '$')}]"

    lines = [
        "# Apply only to an open synthesized design before implementation.",
        "# Review probe existence, clock domain, resource use and timing before rebuilding.",
        "create_debug_core rp_ila ila",
        f"set_property C_DATA_DEPTH {depth} [get_debug_cores rp_ila]",
        "set_property port_width 1 [get_debug_ports rp_ila/CLK]",
        f"set rp_clock {selection(clock_net)}",
        'if {[llength $rp_clock] != 1} { error "ILA clock net missing or ambiguous" }',
        "connect_debug_port rp_ila/CLK $rp_clock",
    ]
    for index, probe in enumerate(probes):
        if index:
            lines.append("create_debug_port rp_ila probe")
        lines.extend([f"set rp_probe_{index} {{}}"])
        for net in probe:
            lines.extend(
                [
                    f"set rp_net {selection(net)}",
                    'if {[llength $rp_net] != 1} { error "ILA probe net missing or ambiguous" }',
                    f"lappend rp_probe_{index} [lindex $rp_net 0]",
                ]
            )
        lines.extend(
            [
                f"set_property port_width {len(probe)} [get_debug_ports rp_ila/PROBE{index}]",
                f"connect_debug_port rp_ila/PROBE{index} $rp_probe_{index}",
            ]
        )
    lines.extend(["opt_design", "write_debug_probes -force debug.ltx"])
    return {
        "executed": False,
        "rebuild_required": True,
        "hardware_capture_supported": False,
        "script": "\n".join(lines) + "\n",
        "net_existence_verified": False,
        "note": "ILA insertion changes the design and can change resource use and timing; "
        "this plan neither instruments a live image nor captures real hardware",
    }
