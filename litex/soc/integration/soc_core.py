#!/usr/bin/env python3
import os
import struct
import inspect
import json
import math
from operator import itemgetter

from migen import *

from litex.build.tools import deprecated_warning

from litex.soc.cores import identifier, timer, uart
from litex.soc.cores.cpu import *
from litex.soc.interconnect.csr import *
from litex.soc.interconnect import wishbone, csr_bus, wishbone2csr


__all__ = [
    "mem_decoder",
    "get_mem_data",
    "csr_map_update",
    "SoCCore",
    "soc_core_args",
    "soc_core_argdict"
]


CPU_VARIANTS = {
    # "official name": ["alias 1", "alias 2"],
    "minimal" : ["min",],
    "lite" : ["light", "zephyr", "nuttx"],
    "standard": [None, "std"],
    "full": [],
    "linux" : [],
}
CPU_VARIANTS_EXTENSIONS = ["debug"]


class InvalidCPUVariantError(ValueError):
    def __init__(self, variant):
        msg = """\
Invalid cpu_variant value: {}

Possible Values:
""".format(variant)
        for k, v in CPU_VARIANTS.items():
            msg += " - {} (aliases: {})\n".format(k, ", ".join(str(s) for s in v))
        ValueError.__init__(self, msg)


class InvalidCPUExtensionError(ValueError):
    def __init__(self, variant):
        msg = """\
Invalid extension in cpu_variant value: {}

Possible Values:
""".format(variant)
        for e in CPU_VARIANTS_EXTENSIONS:
            msg += " - {}\n".format(e)
        ValueError.__init__(self, msg)



def version(with_time=True):
    import datetime
    import time
    if with_time:
        return datetime.datetime.fromtimestamp(
                time.time()).strftime("%Y-%m-%d %H:%M:%S")
    else:
        return datetime.datetime.fromtimestamp(
                time.time()).strftime("%Y-%m-%d")


def mem_decoder(address, start=26, end=29):
    return lambda a: a[start:end] == ((address >> (start+2)) & (2**(end-start))-1)


def get_mem_data(filename_or_regions, endianness="big", mem_size=None):
    # create memory regions
    if isinstance(filename_or_regions, dict):
        regions = filename_or_regions
    else:
        filename = filename_or_regions
        _, ext = os.path.splitext(filename)
        if ext == ".json":
            f = open(filename, "r")
            regions = json.load(f)
            f.close()
        else:
            regions = {filename: "0x00000000"}

    # determine data_size
    data_size = 0
    for filename, base in regions.items():
        data_size = max(int(base, 16) + os.path.getsize(filename), data_size)
    assert data_size > 0
    if mem_size is not None:
        assert data_size < mem_size, (
            "file is too big: {}/{} bytes".format(
             data_size, mem_size))

    # fill data
    data = [0]*math.ceil(data_size/4)
    for filename, base in regions.items():
        with open(filename, "rb") as f:
            i = 0
            while True:
                w = f.read(4)
                if not w:
                    break
                if len(w) != 4:
                    for _ in range(len(w), 4):
                        w += b'\x00'
                if endianness == "little":
                    data[int(base, 16)//4 + i] = struct.unpack("<I", w)[0]
                else:
                    data[int(base, 16)//4 + i] = struct.unpack(">I", w)[0]
                i += 1
    return data


class ReadOnlyDict(dict):
    def __readonly__(self, *args, **kwargs):
        raise RuntimeError("Cannot modify ReadOnlyDict")
    __setitem__ = __readonly__
    __delitem__ = __readonly__
    pop = __readonly__
    popitem = __readonly__
    clear = __readonly__
    update = __readonly__
    setdefault = __readonly__
    del __readonly__


def csr_map_update(csr_map, csr_peripherals):
    csr_map.update(dict((n, v)
        for v, n in enumerate(csr_peripherals, start=max(csr_map.values()) + 1)))


class SoCController(Module, AutoCSR):
    def __init__(self):
        self._reset = CSR()
        self._scratch = CSRStorage(32, reset=0x12345678)
        self._bus_errors = CSRStatus(32)

        # # #

        # reset
        self.reset = Signal()
        self.comb += self.reset.eq(self._reset.re)

        # bus errors
        self.bus_error = Signal()
        bus_errors = Signal(32)
        self.sync += \
            If(bus_errors != (2**len(bus_errors)-1),
                If(self.bus_error,
                    bus_errors.eq(bus_errors + 1)
                )
            )
        self.comb += self._bus_errors.status.eq(bus_errors)


class SoCCore(Module):
    csr_map = {}
    interrupt_map = {}
    mem_map = {
        "rom":      0x00000000,  # (default shadow @0x80000000)
        "sram":     0x10000000,  # (default shadow @0x90000000)
        "main_ram": 0x40000000,  # (default shadow @0xc0000000)
        "csr":      0x60000000,  # (default shadow @0xe0000000)
    }
    def __init__(self, platform, clk_freq,
                cpu_type="vexriscv", cpu_reset_address=0x00000000, cpu_variant=None,
                integrated_rom_size=0, integrated_rom_init=[],
                integrated_sram_size=4096, integrated_sram_init=[],
                integrated_main_ram_size=0, integrated_main_ram_init=[],
                shadow_base=0x80000000,
                csr_data_width=8, csr_address_width=14,
                with_uart=True, uart_name="serial", uart_baudrate=115200, uart_stub=False,
                ident="", ident_version=False,
                wishbone_timeout_cycles=1e6,
                with_timer=True,
                with_ctrl=True):
        self.config = dict()

        self.platform = platform
        self.clk_freq = clk_freq

        self.soc_csr_map = {}
        self.soc_interrupt_map = {}
        self.soc_mem_map = self.mem_map

        # FIXME: RocketChip reserves the first 256Mbytes for internal use
        # remap rom to 0x10000000, sram to 0x20000000
        if cpu_type == "rocket":
            self.soc_mem_map["rom"]  = 0x10000000
            self.soc_mem_map["sram"] = 0x20000000

        if cpu_type == "None":
            cpu_type = None
        self.cpu_type = cpu_type

        # Support the old style which used underscore for separator
        if cpu_variant is None:
            cpu_variant = "standard"
        cpu_variant = cpu_variant.replace('_', '+')
        # Check for valid CPU variants.
        cpu_variant_processor, *cpu_variant_ext = cpu_variant.split('+')
        for key, values in CPU_VARIANTS.items():
            if cpu_variant_processor not in [key,]+values:
                continue
            self.cpu_variant = key
            break
        else:
            raise InvalidCPUVariantError(cpu_variant)

        # Check for valid CPU extensions.
        for ext in sorted(cpu_variant_ext):
            if ext not in CPU_VARIANTS_EXTENSIONS:
                raise InvalidCPUExtensionError(cpu_variant)
            self.cpu_variant += "+"+ext

        if integrated_rom_size:
            cpu_reset_address = self.soc_mem_map["rom"]
        self.cpu_reset_address = cpu_reset_address
        self.config["CPU_RESET_ADDR"] = self.cpu_reset_address

        self.integrated_rom_size = integrated_rom_size
        self.integrated_rom_initialized = integrated_rom_init != []
        self.integrated_sram_size = integrated_sram_size
        self.integrated_main_ram_size = integrated_main_ram_size

        self.with_uart = with_uart
        self.uart_baudrate = uart_baudrate

        self.shadow_base = shadow_base

        self.wishbone_timeout_cycles = wishbone_timeout_cycles

        self.csr_data_width = csr_data_width
        self.csr_address_width = csr_address_width

        self.with_ctrl = with_ctrl

        self._memory_regions = []  # list of (name, origin, length)
        self._csr_regions = []  # list of (name, origin, busword, csr_list/Memory)
        self._constants = []  # list of (name, value)

        self._wb_masters = []
        self._wb_slaves = []
        self._csr_masters = []

        # add user csrs
        for _name, _id in self.csr_map.items():
            self.add_csr(_name, _id)

        if with_ctrl:
            self.submodules.ctrl = SoCController()
            self.add_csr("ctrl", allow_user_defined=True)

        if cpu_type is not None:
            if cpu_type == "lm32":
                self.add_cpu(lm32.LM32(platform, self.cpu_reset_address, self.cpu_variant))
            elif cpu_type == "mor1kx" or cpu_type == "or1k":
                if cpu_type == "or1k":
                    deprecated_warning("SoCCore's \"cpu-type\" to \"mor1kx\"")
                self.add_cpu(mor1kx.MOR1KX(platform, self.cpu_reset_address, self.cpu_variant))
            elif cpu_type == "picorv32":
                self.add_cpu(picorv32.PicoRV32(platform, self.cpu_reset_address, self.cpu_variant))
            elif cpu_type == "vexriscv":
                self.add_cpu(vexriscv.VexRiscv(platform, self.cpu_reset_address, self.cpu_variant))
            elif cpu_type == "minerva":
                self.add_cpu(minerva.Minerva(platform, self.cpu_reset_address, self.cpu_variant))
            elif cpu_type == "rocket":
                self.add_cpu(rocket.RocketRV64(platform, self.cpu_reset_address, self.cpu_variant))
            else:
                raise ValueError("Unsupported CPU type: {}".format(cpu_type))
            self.add_csr("cpu", allow_user_defined=True)
            self.add_wb_master(self.cpu.ibus)
            self.add_wb_master(self.cpu.dbus)
            if with_ctrl:
                self.comb += self.cpu.reset.eq(self.ctrl.reset)
            # add cpu reserved interrupts
            for _name, _id in self.cpu.reserved_interrupts.items():
                self.add_interrupt(_name, _id)

        # add user interrupts
        for _name, _id in self.interrupt_map.items():
            self.add_interrupt(_name, _id)

        self.config["CPU_TYPE"] = str(cpu_type).upper()
        if self.cpu_variant:
            self.config["CPU_VARIANT"] = str(cpu_type).upper()

        if integrated_rom_size:
            self.submodules.rom = wishbone.SRAM(integrated_rom_size, read_only=True, init=integrated_rom_init)
            self.register_rom(self.rom.bus, integrated_rom_size)

        if integrated_sram_size:
            self.submodules.sram = wishbone.SRAM(integrated_sram_size, init=integrated_sram_init)
            self.register_mem("sram", self.soc_mem_map["sram"], self.sram.bus, integrated_sram_size)

        # Note: Main Ram can be used when no external SDRAM is available and use SDRAM mapping.
        if integrated_main_ram_size:
            self.submodules.main_ram = wishbone.SRAM(integrated_main_ram_size, init=integrated_main_ram_init)
            self.register_mem("main_ram", self.soc_mem_map["main_ram"], self.main_ram.bus, integrated_main_ram_size)

        self.submodules.wishbone2csr = wishbone2csr.WB2CSR(
            bus_csr=csr_bus.Interface(csr_data_width, csr_address_width))
        self.add_csr_master(self.wishbone2csr.csr)
        self.config["CSR_DATA_WIDTH"] = csr_data_width
        self.add_constant("CSR_DATA_WIDTH", csr_data_width)
        self.register_mem("csr", self.soc_mem_map["csr"], self.wishbone2csr.wishbone)

        if with_uart:
            if uart_stub:
                self.submodules.uart  = uart.UARTStub()
            else:
                self.submodules.uart_phy = uart.RS232PHY(platform.request(uart_name), clk_freq, uart_baudrate)
                self.submodules.uart = ResetInserter()(uart.UART(self.uart_phy))
            self.add_csr("uart_phy", allow_user_defined=True)
            self.add_csr("uart", allow_user_defined=True)
            self.add_interrupt("uart", allow_user_defined=True)

        if ident:
            if ident_version:
                ident = ident + " " + version()
            self.submodules.identifier = identifier.Identifier(ident)
            self.add_csr("identifier_mem", allow_user_defined=True)
        self.config["CLOCK_FREQUENCY"] = int(clk_freq)
        self.add_constant("SYSTEM_CLOCK_FREQUENCY", int(clk_freq))

        if with_timer:
            self.submodules.timer0 = timer.Timer()
            self.add_csr("timer0", allow_user_defined=True)
            self.add_interrupt("timer0", allow_user_defined=True)

    def add_cpu(self, cpu):
        if self.finalized:
            raise FinalizeError
        if hasattr(self, "cpu"):
            raise NotImplementedError("More than one CPU is not supported")
        self.submodules.cpu = cpu

    def add_cpu_or_bridge(self, cpu_or_bridge):
        deprecated_warning("SoCCore's \"add_cpu_or_bridge\" call to \"add_cpu\"")
        self.add_cpu(cpu_or_bridge)
        self.cpu_or_bridge = self.cpu

    def add_interrupt(self, interrupt_name, interrupt_id=None, allow_user_defined=False):
        # check that interrupt_name is not already used
        if interrupt_name in self.soc_interrupt_map.keys():
            if allow_user_defined:
                return
            else:
                raise ValueError("Interrupt conflit, {} name already used".format(interrupt_name))

        # check that interrupt_id is in range
        if interrupt_id is not None and interrupt_id >= 32:
            raise ValueError("{} Interrupt ID out of range ({}, max=31)".format(
                interrupt_name, interrupt_id))

        # interrupt_id not provided: allocate interrupt to the first available id
        if interrupt_id is None:
            for n in range(32):
                if n not in self.soc_interrupt_map.values():
                    self.soc_interrupt_map.update({interrupt_name: n})
                    return
            raise ValueError("No more space to allocate {} interrupt".format(interrupt_name))
        # interrupt_id provided: check that interrupt_id is not already used and add interrupt
        else:
            for _name, _id in self.soc_interrupt_map.items():
                if interrupt_id == _id:
                    raise ValueError("Interrupt conflict, {} already used by {} interrupt".format(
                        interrupt_id, _name))
            self.soc_interrupt_map.update({interrupt_name: interrupt_id})

    def add_csr(self, csr_name, csr_id=None, allow_user_defined=False):
        # check that csr_name is not already used
        if csr_name in self.soc_csr_map.keys():
            if allow_user_defined:
                return
            else:
                raise ValueError("CSR conflit, {} name already used".format(csr_name))

        # check that csr_id is in range
        if csr_id is not None and csr_id >= 2**self.csr_address_width:
            raise ValueError("{} CSR ID out of range ({}, max=31)".format(
                csr_name, csr_id))

        # csr_id not provided: allocate csr to the first available id
        if csr_id is None:
            for n in range(2**self.csr_address_width):
                if n not in self.soc_csr_map.values():
                    self.soc_csr_map.update({csr_name: n})
                    return
            raise ValueError("No more space to allocate {} csr".format(csr_name))
        # csr_id provided: check that csr_id is not already used and add csr
        else:
            for _name, _id in self.soc_csr_map.items():
                if csr_id == _id:
                    raise ValueError("CSR conflict, {} already used by {} csr".format(
                        csr_id, _name))
            self.soc_csr_map.update({csr_name: csr_id})

    def initialize_rom(self, data):
        self.rom.mem.init = data

    def add_wb_master(self, wbm):
        if self.finalized:
            raise FinalizeError
        self._wb_masters.append(wbm)

    def add_wb_slave(self, address_decoder, interface):
        if self.finalized:
            raise FinalizeError
        self._wb_slaves.append((address_decoder, interface))

    def add_csr_master(self, csrm):
        # CSR masters are not arbitrated, use this with precaution.
        if self.finalized:
            raise FinalizeError
        self._csr_masters.append(csrm)

    def add_memory_region(self, name, origin, length):
        def in_this_region(addr):
            return addr >= origin and addr < origin + length
        for n, o, l in self._memory_regions:
            if n == name or in_this_region(o) or in_this_region(o+l-1):
                raise ValueError("Memory region conflict between {} and {}".format(n, name))

        self._memory_regions.append((name, origin, length))

    def register_mem(self, name, address, interface, size=None):
        self.add_wb_slave(mem_decoder(address), interface)
        if size is not None:
            self.add_memory_region(name, address, size)

    def register_rom(self, interface, rom_size=0xa000):
        self.add_wb_slave(mem_decoder(self.soc_mem_map["rom"]), interface)
        self.add_memory_region("rom", self.cpu_reset_address, rom_size)

    def get_memory_regions(self):
        return self._memory_regions

    def check_csr_range(self, name, addr):
        if addr >= 1<<(self.csr_address_width+2):
            raise ValueError("{} CSR out of range, increase csr_address_width".format(name))

    def check_csr_region(self, name, origin):
        for n, o, l, obj in self._csr_regions:
            if n == name or o == origin:
                raise ValueError("CSR region conflict between {} and {}".format(n, name))

    def add_csr_region(self, name, origin, busword, obj):
        self.check_csr_region(name, origin)
        self._csr_regions.append((name, origin, busword, obj))

    def get_csr_regions(self):
        return self._csr_regions

    def add_constant(self, name, value=None):
        self._constants.append((name, value))

    def get_constants(self):
        r = []
        for _name, _id in sorted(self.soc_interrupt_map.items()):
            r.append((_name.upper() + "_INTERRUPT", _id))
        r += self._constants
        return r

    def get_csr_dev_address(self, name, memory):
        if memory is not None:
            name = name + "_" + memory.name_override
        try:
            return self.soc_csr_map[name]
        except KeyError as e:
            msg = "Undefined \"{}\" CSR.\n".format(name)
            msg += "Avalaible CSRs in {} ({}):\n".format(
                self.__class__.__name__, inspect.getfile(self.__class__))
            for k in sorted(self.soc_csr_map.keys()):
                msg += "- {}\n".format(k)
            raise RuntimeError(msg)
        except ValueError:
            return None

    def do_finalize(self):
        registered_mems = {regions[0] for regions in self._memory_regions}
        if self.cpu_type is not None:
            for mem in "rom", "sram":
                if mem not in registered_mems:
                    raise FinalizeError("CPU needs a {} to be registered with SoC.register_mem()".format(mem))

        # Wishbone
        if len(self._wb_masters):
            self.submodules.wishbonecon = wishbone.InterconnectShared(self._wb_masters,
                self._wb_slaves, register=True, timeout_cycles=self.wishbone_timeout_cycles)
            if self.with_ctrl and (self.wishbone_timeout_cycles is not None):
                self.comb += self.ctrl.bus_error.eq(self.wishbonecon.timeout.error)

        # CSR
        self.submodules.csrbankarray = csr_bus.CSRBankArray(self,
            self.get_csr_dev_address,
            data_width=self.csr_data_width, address_width=self.csr_address_width)
        self.submodules.csrcon = csr_bus.InterconnectShared(
                self._csr_masters, self.csrbankarray.get_buses())
        for name, csrs, mapaddr, rmap in self.csrbankarray.banks:
            self.check_csr_range(name, 0x800*mapaddr)
            self.add_csr_region(name, (self.soc_mem_map["csr"] + 0x800*mapaddr) | self.shadow_base, self.csr_data_width, csrs)
        for name, memory, mapaddr, mmap in self.csrbankarray.srams:
            self.check_csr_range(name, 0x800*mapaddr)
            self.add_csr_region(name + "_" + memory.name_override, (self.soc_mem_map["csr"] + 0x800*mapaddr) | self.shadow_base, self.csr_data_width, memory)
        for name, constant in self.csrbankarray.constants:
            self._constants.append(((name + "_" + constant.name).upper(), constant.value.value))
        for name, value in sorted(self.config.items(), key=itemgetter(0)):
            self._constants.append(("CONFIG_" + name.upper(), value))

        # Interrupts
        if hasattr(self, "cpu"):
            if hasattr(self.cpu, "interrupt"):
                for _name, _id in sorted(self.soc_interrupt_map.items()):
                    if _name in self.cpu.reserved_interrupts.keys():
                        continue
                    if hasattr(self, _name):
                        module = getattr(self, _name)
                        assert hasattr(module, 'ev'), "Submodule %s does not have EventManager (xx.ev) module" % _name
                        self.comb += self.cpu.interrupt[_id].eq(module.ev.irq)

    def build(self, *args, **kwargs):
        return self.platform.build(self, *args, **kwargs)


def soc_core_args(parser):
    parser.add_argument("--cpu-type", default=None,
                        help="select CPU: lm32, or1k, picorv32, vexriscv, minerva")
    parser.add_argument("--cpu-variant", default=None,
                        help="select CPU variant")
    parser.add_argument("--integrated-rom-size", default=None, type=int,
                        help="size/enable the integrated (BIOS) ROM")
    parser.add_argument("--integrated-main-ram-size", default=None, type=int,
                        help="size/enable the integrated main RAM")
    parser.add_argument("--uart-stub", default=False, type=bool,
                        help="enable uart stub")


def soc_core_argdict(args):
    r = dict()
    for a in [
        "cpu_type",
        "cpu_variant",
        "integrated_rom_size",
        "integrated_main_ram_size",
        "uart_stub"]:
        arg = getattr(args, a)
        if arg is not None:
            r[a] = arg
    return r
