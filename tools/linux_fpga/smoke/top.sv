module top (
    input  logic       clk,
    input  logic [7:0] a,
    input  logic [7:0] b,
    output logic [8:0] sum
);
    always_ff @(posedge clk) begin
        sum <= a + b;
    end
endmodule
