# This file is Copyright (c) 2014 Florent Kermarrec <florent@enjoy-digital.fr>
# License: BSD

import os, subprocess, sys

from migen.fhdl.std import *
from migen.fhdl.structure import _Fragment
from mibuild.generic_platform import *

from mibuild import tools, xilinx_common

def _format_constraint(c):
	if isinstance(c, Pins):
		return "set_property LOC " + c.identifiers[0]
	elif isinstance(c, IOStandard):
		return "set_property IOSTANDARD " + c.name 
	elif isinstance(c, Drive):
		return "set_property DRIVE " + str(c.strength)
	elif isinstance(c, Misc):
		return "set_property " + c.misc.replace("=", " ")

def _format_xdc(signame, pin, others, resname):
	fmt_c = [_format_constraint(c) for c in ([Pins(pin)] + others)]
	fmt_r = resname[0] + ":" + str(resname[1])
	if resname[2] is not None:
		fmt_r += "." + resname[2]
	r = " ## %s\n" %fmt_r 
	for c in fmt_c:
		r += c + " [get_ports " + signame + "]\n"
	return r

def _build_xdc(named_sc, named_pc):
	r = ""
	for sig, pins, others, resname in named_sc:
		if len(pins) > 1:
			for i, p in enumerate(pins):
				r += _format_xdc(sig + "[" + str(i) + "]", p, others, resname)
		else:
			r += _format_xdc(sig, pins[0], others, resname)
	if named_pc:
		r += "\n" + "\n\n".join(named_pc)
	return r

def _build_files(device, sources, vincpaths, build_name):
	tcl = []
	for filename, language in sources:
		tcl.append("add_files " + filename.replace("\\", "/"))

	tcl.append("read_xdc %s.xdc" %build_name)
	tcl.append("synth_design -top top -part %s -include_dirs {%s}" %(device, " ".join(vincpaths)))
	tcl.append("report_utilization -file %s_utilization_synth.rpt" %(build_name))
	tcl.append("place_design")
	tcl.append("report_utilization -file %s_utilization_place.rpt" %(build_name))
	tcl.append("report_io -file %s_io.rpt" %(build_name))
	tcl.append("report_control_sets -verbose -file %s_control_sets.rpt" %(build_name))
	tcl.append("report_clock_utilization -file %s_clock_utilization.rpt" %(build_name))
	tcl.append("route_design")
	tcl.append("report_route_status -file %s_route_status.rpt" %(build_name))
	tcl.append("report_drc -file %s_drc.rpt" %(build_name))
	tcl.append("report_timing_summary -file %s_timing.rpt" %(build_name))
	tcl.append("report_power -file %s_power.rpt" %(build_name))
	tcl.append("write_bitstream -force %s.bit " %build_name)
	tcl.append("quit")
	tools.write_to_file(build_name + ".tcl", "\n".join(tcl))

def _run_vivado(build_name, vivado_path, source, ver=None):
	if sys.platform == "win32" or sys.platform == "cygwin":
		source = False
	build_script_contents = "# Autogenerated by mibuild\nset -e\n"
	if source:
		settings = xilinx_common.settings(vivado_path, ver)
		build_script_contents += "source " + settings + "\n"
	build_script_contents += "vivado -mode tcl -source " + build_name + ".tcl\n"
	build_script_file = "build_" + build_name + ".sh"
	tools.write_to_file(build_script_file, build_script_contents, force_unix=True)

	r = subprocess.call(["bash", build_script_file])
	if r != 0:
		raise OSError("Subprocess failed")

class XilinxVivadoPlatform(xilinx_common.XilinxGenericPlatform):
	def build(self, fragment, build_dir="build", build_name="top",
			vivado_path="/opt/Xilinx/Vivado", source=True, run=True):
		tools.mkdir_noerror(build_dir)
		os.chdir(build_dir)

		if not isinstance(fragment, _Fragment):
			fragment = fragment.get_fragment()
		self.finalize(fragment)
		v_src, named_sc, named_pc = self.get_verilog(fragment)
		v_file = build_name + ".v"
		tools.write_to_file(v_file, v_src)
		sources = self.sources + [(v_file, "verilog")]
		_build_files(self.device, sources, self.verilog_include_paths, build_name)
		tools.write_to_file(build_name + ".xdc", _build_xdc(named_sc, named_pc))
		if run:
			_run_vivado(build_name, vivado_path, source)
		
		os.chdir("..")

	def add_period_constraint(self, clk, period):
		self.add_platform_command("""create_clock -name {clk} -period """ +\
			str(period) + """ [get_ports {clk}]""", clk=clk)
