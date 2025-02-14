import subprocess
import unittest
import os

from migen import *

from litex.soc.integration.builder import *


RUNNING_ON_TRAVIS = (os.getenv('TRAVIS', 'false').lower() == 'true')


def build_test(socs):
    errors = 0
    for soc in socs:
        os.system("rm -rf build")
        builder = Builder(soc, output_dir="./build", compile_software=False, compile_gateware=False)
        builder.build()
        errors += not os.path.isfile("./build/gateware/top.v")
    os.system("rm -rf build")
    return errors


class TestTargets(unittest.TestCase):
    # Altera boards
    def test_de0nano(self):
        from litex.boards.targets.de0nano import BaseSoC
        errors = build_test([BaseSoC()])
        self.assertEqual(errors, 0)

    # Xilinx boards
    # Spartan-6
    def test_minispartan6(self):
        from litex.boards.targets.minispartan6 import BaseSoC
        errors = build_test([BaseSoC()])
        self.assertEqual(errors, 0)

    # Artix-7
    def test_arty(self):
        from litex.boards.targets.arty import BaseSoC, EthernetSoC
        errors = build_test([BaseSoC(), EthernetSoC()])
        self.assertEqual(errors, 0)

    def test_netv2(self):
        from litex.boards.targets.netv2 import BaseSoC, EthernetSoC
        errors = build_test([BaseSoC(), EthernetSoC()])
        self.assertEqual(errors, 0)

    def test_nexys4ddr(self):
        from litex.boards.targets.nexys4ddr import BaseSoC
        errors = build_test([BaseSoC()])
        self.assertEqual(errors, 0)

    def test_nexys_video(self):
        from litex.boards.targets.nexys_video import BaseSoC, EthernetSoC
        errors = build_test([BaseSoC(), EthernetSoC()])
        self.assertEqual(errors, 0)

    # Kintex-7
    def test_genesys2(self):
        from litex.boards.targets.genesys2 import BaseSoC, EthernetSoC
        errors = build_test([BaseSoC(), EthernetSoC()])
        self.assertEqual(errors, 0)

    def test_kc705(self):
        from litex.boards.targets.kc705 import BaseSoC, EthernetSoC
        errors = build_test([BaseSoC(), EthernetSoC()])
        self.assertEqual(errors, 0)

    # Kintex-Ultrascale
    def test_kcu105(self):
        from litex.boards.targets.kcu105 import BaseSoC
        errors = build_test([BaseSoC()])
        self.assertEqual(errors, 0)

    # Lattice boards
    # ECP5
    def test_versa_ecp5(self):
        from litex.boards.targets.versa_ecp5 import BaseSoC
        errors = build_test([BaseSoC()])
        self.assertEqual(errors, 0)

    def test_ulx3s(self):
        from litex.boards.targets.ulx3s import BaseSoC
        errors = build_test([BaseSoC()])
        self.assertEqual(errors, 0)

    # Build simple design for all platforms
    def test_simple(self):
        platforms = []
        # Xilinx
        platforms += ["minispartan6", "sp605"]                     # Spartan6
        platforms += ["arty", "netv2", "nexys4ddr", "nexys_video", # Artix7
                      "ac701"]
        platforms += ["kc705", "genesys2"]                         # Kintex7
        platforms += ["kcu105"]                                    # Kintex Ultrascale

        # Altera/Intel
        platforms += ["de0nano", "de2_115"]                        # Cyclone4
        platforms += ["de1soc"]                                    # Cyclone5

        # Lattice
        platforms += ["tinyfpga_bx"]                               # iCE40
        platforms += ["machxo3"]                                   # MachXO3
        platforms += ["versa_ecp3"]                                # ECP3
        platforms += ["versa_ecp5", "ulx3s"]                       # ECP5

        # Microsemi
        platforms += ["avalanche"]                                 # PolarFire

        for p in platforms:
            with self.subTest(platform=p):
                cmd = """\
litex/boards/targets/simple.py litex.boards.platforms.{p} \
    --cpu-type=vexriscv     \
    --no-compile-software   \
    --no-compile-gateware   \
    --uart-stub=True        \
""".format(p=p)
                subprocess.check_call(cmd, shell=True)

    def run_variants(self, cpu, variants):
        for v in variants:
            with self.subTest(cpu=cpu, variant=v):
                self.run_variant(cpu, v)

    def run_variant(self, cpu, variant):
        cmd = """\
litex/boards/targets/simple.py litex.boards.platforms.arty \
    --cpu-type={c}          \
    --cpu-variant={v}       \
    --no-compile-software   \
    --no-compile-gateware   \
    --uart-stub=True        \
""".format(c=cpu, v=variant)
        subprocess.check_output(cmd, shell=True)

    # Build some variants for the arty platform to make sure they work.
    def test_variants_riscv(self):
        cpu_variants = {
            'picorv32': ('standard', 'minimal'),
            'vexriscv': ('standard', 'minimal', 'lite', 'lite+debug', 'full+debug'),
            'minerva': ('standard',),
        }
        for cpu, variants in cpu_variants.items():
            self.run_variants(cpu, variants)

    #def test_bad_variants(self):
    #    with self.assertRaises(subprocess.CalledProcessError):
    #        self.run_variant('vexriscv', 'bad')

    #def test_bad_variant_extension(self):
    #    with self.assertRaises(subprocess.CalledProcessError):
    #        self.run_variant('vexriscv', 'standard+bad')

    @unittest.skipIf(RUNNING_ON_TRAVIS, "No lm32 toolchain on Travis-CI")
    def test_variants_lm32(self):
        self.run_variants('lm32', ('standard', 'minimal', 'lite'))

    @unittest.skipIf(RUNNING_ON_TRAVIS, "No or1k toolchain on Travis-CI")
    def test_variants_or1k(self):
        self.run_variants('or1k', ('standard', 'linux'))
