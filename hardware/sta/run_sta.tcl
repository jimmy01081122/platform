# OpenSTA sign-off-lite: real STA engine + liberty wireload model (fanout-based
# net RC), reg-to-reg setup timing + Fmax + power. NOT full sign-off (no extracted
# SPEF from place&route), but a real STA tool with wire load -> higher evidence than
# the S6+ cell-only abc estimate.
#
# Args via env: LIB, NETLIST, TOP, PERIOD (ns, probe clock), WLM (wire load model).
set lib     $env(LIB)
set netlist $env(NETLIST)
set top     $env(TOP)
set period  $env(PERIOD)
set wlm     $env(WLM)

read_liberty $lib
read_verilog $netlist
link_design $top

# Fanout-based wire load (captures high-fanout net RC, e.g. the idx->32-wide decode).
set_wire_load_mode top
set_wire_load_model -name $wlm

# Ideal clock (no PLL jitter / OCV modeled here).
create_clock -name clk -period $period [get_ports clk]

# I/O delay = 0 so the reported worst path reflects the CORE reg-to-reg critical
# path (I/O paths keep full-period budget). set_load models a nominal output cap.
set_input_delay  0 -clock clk [all_inputs]
set_output_delay 0 -clock clk [all_outputs]
set_load 5.0 [all_outputs]

puts "==== reg-to-reg critical path (worst) ===="
report_checks -from [all_registers] -to [all_registers] -path_delay max -digits 4

puts "==== WNS/TNS (core, reg-to-reg dominated) ===="
set wns [sta::worst_slack -max]
set tns [sta::total_negative_slack -max]
puts "WNS $wns"
puts "TNS $tns"
# path_delay = period - WNS ; Fmax = 1/path_delay
set path_delay [expr {$period - $wns}]
if {$path_delay > 0} {
  puts [format "PATH_DELAY_NS %.4f" $path_delay]
  puts [format "FMAX_MHZ %.2f" [expr {1000.0 / $path_delay}]]
}

puts "==== POWER (default switching activity) ===="
# No VCD -> default activity: rough but methodology-grounded estimate.
catch { set_power_activity -input -activity 0.2 -duty 0.5 } e1
catch { report_power -digits 6 } e2
if {$e2 ne ""} { puts "power_report_error: $e2" }

exit
