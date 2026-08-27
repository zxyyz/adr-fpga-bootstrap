set required_part xczu47dr-ffve1156-2-i
read_verilog -sv top.sv
synth_design -top top -part $required_part
set cell_count [llength [get_cells -hierarchical]]
if {$cell_count < 1} {
    error "ADR-EDA-VIVADO-SYNTH-EMPTY: synthesis produced no cells."
}
report_utilization -file synth_utilization.rpt
write_checkpoint -force synth_smoke.dcp
puts "ADR_SYNTH_OK part=$required_part cells=$cell_count"
exit 0
