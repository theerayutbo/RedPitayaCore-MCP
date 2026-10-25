`timescale 1ns/1ps

module simple_counter #(
    parameter int unsigned WIDTH = 8
) (
    input  logic                 clk,
    input  logic                 reset_n,
    output logic [WIDTH-1:0]     count
);
    always_ff @(posedge clk) begin
        if (!reset_n) begin
            count <= '0;
        end else begin
            count <= count + 1'b1;
        end
    end
endmodule
