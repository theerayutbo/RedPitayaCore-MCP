from redpitayacore_mcp.config import Settings
from redpitayacore_mcp.server import build_server


def test_instructions_warn_about_buffered_rx_and_point_to_the_radio_skill():
    """Agents see these instructions with every tool list. Measured on a board: tool calls
    are seconds apart, so a read after a pause returns old buffered samples, and a burst
    started after a drain was mostly missed. The instructions must say so and name the
    skill that holds the recipes."""
    text = build_server(Settings.from_env({})).instructions or ""
    assert "oldest buffered samples" in text
    assert "start a long burst first" in text
    assert "redpitaya-core-radio-ops" in text
    assert "acknowledgement" in text  # the existing safety text is still there
