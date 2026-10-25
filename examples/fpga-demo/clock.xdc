# Timing-only example. This file intentionally contains no package-pin or I/O
# standard assignments and is not a complete constraint set for a STEMlab board.
create_clock -name example_clk -period 8.000 [get_ports clk]
