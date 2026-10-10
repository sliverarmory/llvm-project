"""Regression checks for isolated function evidence from export-only PE files."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import machine_code
from benchmark_rust import machine_mnemonics


class IsolatedInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binary = Path(self.directory.name) / "fixture.exe"
        self.binary.write_bytes(b"MZ")
        machine_code._coff_unwind_ranges.cache_clear()

    def read(self, disassembly, headers, *, raw="pipeline_probe", required=True):
        def run(command, **kwargs):
            return SimpleNamespace(returncode=0, stderr="", stdout=(
                headers if "--unwind-info" in command else disassembly))
        with patch.object(machine_code.subprocess, "run", side_effect=run):
            return machine_code.isolated_instructions(
                "llvm-objdump", self.binary, raw, required=required)

    def test_following_function_cannot_satisfy_probe_effect(self):
        headers = ("ImageBase 0000000140000000\nFunction Table:\n"
                   "  Start Address: 0x1000\n  End Address: 0x1006\n")
        probe = ("0000000140001000 <pipeline_probe>:\n"
                 "140001000: movl %ecx, %eax\n"
                 "140001002: addl $1, %eax\n"
                 "140001005: retq\n")
        ordinary = probe + "140001010: addl %eax, %eax\n140001012: retq\n"
        changed_other = probe + "140001010: xorl $123, %eax\n140001015: retq\n"
        self.assertNotEqual(ordinary, changed_other)
        self.assertEqual(self.read(ordinary, headers), self.read(changed_other, headers))
        def run(command, **kwargs):
            return SimpleNamespace(returncode=0, stderr="", stdout=(
                headers if "--unwind-info" in command else changed_other))
        with patch.object(machine_code.subprocess, "run", side_effect=run), \
                patch("benchmark_rust.platform.system", return_value="Windows"):
            self.assertEqual(machine_mnemonics("llvm-objdump", self.binary),
                             ["movl", "addl", "retq"])

    def test_branching_leaf_requires_exact_metadata(self):
        dump = ("0000000140001000 <pipeline_probe>:\n"
                "140001000: testl %ecx, %ecx\n"
                "140001002: jle 0x14000100a <pipeline_probe+0xa>\n"
                "140001004: movl %ecx, %eax\n"
                "140001006: retq\n"
                "14000100a: xorl %eax, %eax\n"
                "14000100c: retq\n"
                "140001010: addl $123, %eax\n"
                "140001015: retq\n")
        with self.assertRaisesRegex(RuntimeError, "needs an unwind range"):
            self.read(dump, "ImageBase 0000000140000000\n")

    def test_private_tail_call_cannot_include_following_function(self):
        dump = ("0000000140001000 <pipeline_probe>:\n"
                "140001000: jmp 0x140001010 <pipeline_probe+0x10>\n"
                "140001005: nopw (%rax)\n"
                "140001010: leal (%rcx,%rcx), %eax\n"
                "140001013: retq\n")
        with self.assertRaisesRegex(RuntimeError, "needs an unwind range"):
            self.read(dump, "ImageBase 0000000140000000\n")

    def test_unwind_range_bounds_private_tail_call_exactly(self):
        dump = ("0000000140001000 <pipeline_probe>:\n"
                "140001000: pushq %rbp\n"
                "140001001: movq %rsp, %rbp\n"
                "140001004: popq %rbp\n"
                "140001005: jmp 0x140001010 <pipeline_probe+0x10>\n"
                "14000100a: nopw (%rax)\n"
                "140001010: leal (%rcx,%rcx), %eax\n"
                "140001013: retq\n")
        headers = ("ImageBase 0000000140000000\nFunction Table:\n"
                   "  Start Address: 0x1000\n  End Address: 0x100a\n")
        code = self.read(dump, headers)
        self.assertEqual(len(code), 4)
        self.assertTrue(code[-1].startswith("jmp "))

    def test_straight_line_leaf_stops_before_adjacent_code(self):
        dump = ("0000000140001000 <pipeline_probe>:\n"
                "140001000: movl %ecx, %eax\n"
                "140001002: retq\n"
                "140001010: xorl %eax, %eax\n"
                "140001012: retq\n")
        self.assertEqual(self.read(dump, "ImageBase 0000000140000000\n"),
                         ["movl %ecx, %eax", "retq"])

    def test_unbound_leaf_fails(self):
        with self.assertRaisesRegex(RuntimeError, "needs an unwind range"):
            self.read("0000000140001000 <pipeline_probe>:\n"
                      "140001000: jmp 0x140001000 <pipeline_probe>\n",
                      "ImageBase 0000000140000000\n")

    def test_optional_missing_symbol(self):
        self.assertEqual(self.read("", "", required=False), [])
        with self.assertRaisesRegex(RuntimeError, "function pipeline_probe absent"):
            self.read("", "")

    def test_coff_object_keeps_symbol_range(self):
        self.binary.write_bytes(b"\x64\x86")
        dump = ("0000000000000000 <pipeline_probe>:\n"
                "0: movl %ecx, %eax\n"
                "2: retq\n"
                "3: nopl (%rax)\n"
                "0000000000000010 <following>:\n"
                "10: xorl %eax, %eax\n"
                "12: retq\n")
        self.assertEqual(self.read(dump, ""),
                         ["movl %ecx, %eax", "retq", "nopl (%rax)"])

    def test_duplicate_labels_and_aliases_at_function_start(self):
        for magic in (b"MZ", b"\x64\x86"):
            with self.subTest(magic=magic):
                self.binary.write_bytes(magic)
                dump = ("0000000140001000 <pipeline_probe>:\n"
                        "0000000140001000 <pipeline_probe>:\n"
                        "0000000140001000 <alias>:\n"
                        "140001000: movl %ecx, %eax\n"
                        "140001002: retq\n"
                        "0000000140001010 <following>:\n"
                        "140001010: xorl %eax, %eax\n"
                        "140001012: retq\n")
                self.assertEqual(self.read(dump, "ImageBase 0000000140000000\n"),
                                 ["movl %ecx, %eax", "retq"])


if __name__ == "__main__":
    unittest.main()
