set required_part xczu47dr-ffve1156-2-i
set matches [get_parts -quiet $required_part]
if {[llength $matches] != 1} {
    error "ADR-EDA-VIVADO-DEVICE-MISSING: expected exactly one '$required_part' part, found [llength $matches]."
}
puts "ADR_DEVICE_OK=[lindex $matches 0]"
exit 0
