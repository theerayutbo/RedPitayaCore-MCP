import pytest

from redpitayacore_mcp.fpga_profile import hardware_plan, ila_plan, register_map, register_plan


def test_pinned_map_has_exact_widths_and_no_live_access():
    profile = register_map()
    entries = {entry["name"]: entry for entry in profile["registers"]}
    assert entries["rx_fifo_count"]["address"] == "0x41000000"
    assert entries["tx_fifo_count"]["width_bits"] == 16
    assert entries["rx_phase_increment"]["width_bits"] == 32
    assert entries["gpio_ptt"]["address"] == "0x40000002"
    assert profile["live_access_supported"] is False
    assert profile["image_identity_verified"] is False


def test_named_register_plans_never_read_or_write_hardware():
    planned = register_plan("rx_phase_increment", "write", 123, 456)
    assert planned["executed"] is False
    assert planned["expected_value"] == 456
    with pytest.raises(ValueError):
        register_plan("0x40000002", "write", 1, 0)
    with pytest.raises(ValueError):
        register_plan("rx_fifo_count", "write", 1, 0)
    with pytest.raises(ValueError):
        register_plan("gpio_ptt", "write", 256, 0)
    with pytest.raises(ValueError):
        register_plan("gpio_ptt", "write", True, 0)


def test_hardware_plans_preserve_blockers_and_do_not_imply_hash_is_safety():
    for operation in ["load_bitstream", "register_write", "tx_burst"]:
        planned = hardware_plan(operation)
        assert planned["executed"] is False
        assert planned["execution_supported"] is False
        assert planned["hardware_tx_state"] == "unknown_not_read"
        assert planned["blockers"]
    tx = hardware_plan("tx_burst")
    assert "exact-frame server" in " ".join(tx["blockers"])
    with pytest.raises(ValueError):
        hardware_plan("arbitrary_remote_shell")


def test_ila_plan_has_bounded_exact_nets_and_never_programs_hardware():
    planned = ila_plan("clk", [["counter[0]", "counter[1]"], ["valid"]], 1024)
    assert planned["executed"] is False
    assert "create_debug_core" in planned["script"]
    assert "write_debug_probes" in planned["script"]
    assert planned["script"].index("\nopt_design\n") < planned["script"].index("write_debug_probes")
    assert "port_width 2" in planned["script"]
    assert "program_hw" not in planned["script"]
    assert planned["rebuild_required"] is True
    for name in ["clk; exec bad", "[exec bad]", "$secret"]:
        with pytest.raises(ValueError):
            ila_plan(name, [["valid"]], 1024)
    with pytest.raises(ValueError):
        ila_plan("clk", [[]], 1024)
    with pytest.raises(ValueError):
        ila_plan("clk", [["valid"]], 1234)
