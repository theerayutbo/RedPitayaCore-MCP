`timescale 1ns/1ps

module tb_counter;
    localparam int unsigned WIDTH = 8;

    logic clk = 1'b0;
    logic reset_n = 1'b0;
    logic [WIDTH-1:0] count;

    simple_counter #(
        .WIDTH(WIDTH)
    ) dut (
        .clk(clk),
        .reset_n(reset_n),
        .count(count)
    );

    always #4 clk = ~clk;

    initial begin
        repeat (2) @(posedge clk);
        #1;
        if (count !== '0) begin
            $fatal(1, "counter did not reset: count=%0d", count);
        end

        reset_n = 1'b1;
        repeat (5) @(posedge clk);
        #1;
        if (count !== 8'd5) begin
            $fatal(1, "counter mismatch: expected=5 actual=%0d", count);
        end

        reset_n = 1'b0;
        @(posedge clk);
        #1;
        if (count !== '0) begin
            $fatal(1, "counter did not reset after counting: count=%0d", count);
        end

        $display("PASS: reset and counting behavior verified");
        $finish;
    end
endmodule
