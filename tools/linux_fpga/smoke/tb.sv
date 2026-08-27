module tb;
    logic       clk = 1'b0;
    logic [7:0] a = 8'd0;
    logic [7:0] b = 8'd0;
    logic [8:0] sum;

    top dut (.*);

    always #5 clk = ~clk;

    initial begin
        a = 8'd17;
        b = 8'd25;
        repeat (2) @(posedge clk);
        #1;
        if (sum !== 9'd42) begin
            $fatal(1, "ADR-EDA-XSIM-SMOKE: expected 42, got %0d", sum);
        end
        $display("ADR_XSIM_OK sum=%0d", sum);
        $finish;
    end
endmodule
