"""Regression tests for final-binary evidence parsing, without native tools."""

from pathlib import Path
import unittest
from unittest.mock import patch

from binary_evidence import final_section_sizes


# Representative LLVM 23 --section-headers output from Clang-built objects.
# The empty ELF null-section name and trailing empty Type columns caused the
# original multiline expression to combine adjacent rows.
ELF = """
sample.o: file format elf64-x86-64

Sections:
Idx Name            Size     VMA              Type
  0                 00000000 0000000000000000
  1 .strtab         00000085 0000000000000000
  2 .text           0000001b 0000000000000000 TEXT
  3 .rela.text      00000048 0000000000000000
  4 .data           00000004 0000000000000000 DATA
  5 .rodata         00000005 0000000000000000 DATA
  6 .bss            00000004 0000000000000000 BSS
  7 .comment        0000006e 0000000000000000
  8 .note.GNU-stack 00000000 0000000000000000
  9 .eh_frame       00000038 0000000000000000 DATA
 10 .rela.eh_frame  00000018 0000000000000000
 11 .llvm_addrsig   00000003 0000000000000000
 12 .symtab         000000a8 0000000000000000
"""

MACH_O = """
sample.o: file format mach-o arm64

Sections:
Idx Name             Size     VMA              Type
  0 __text           00000024 0000000000000000 TEXT
  1 __data           00000004 0000000000000024 DATA
  2 __const          00000005 0000000000000028 DATA
  3 __common         00000004 0000000000000050 BSS
  4 __compact_unwind 00000020 0000000000000030 DATA
"""

COFF = """
sample.o: file format coff-x86-64

Sections:
Idx Name          Size     VMA              Type
  0 .text         00000016 0000000000000000 TEXT
  1 .data         00000004 0000000000000000 DATA
  2 .bss          00000004 0000000000000000 BSS
  3 .rdata        00000005 0000000000000000 DATA
  4 .debug$S      000000a0 0000000000000000 DATA, DEBUG
  5 .llvm_addrsig 00000003 0000000000000000
"""


class FinalSectionSizesTests(unittest.TestCase):
    def parse(self, output):
        with patch("binary_evidence.checked", return_value=output):
            return final_section_sizes(Path("llvm-objdump"), Path("sample.o"))

    def test_elf_preserves_untyped_rows_and_does_not_merge_null_section(self):
        result = self.parse(ELF)
        self.assertEqual([row["name"] for row in result["sections"]], [
            ".strtab", ".text", ".rela.text", ".data", ".rodata", ".bss",
            ".comment", ".note.GNU-stack", ".eh_frame", ".rela.eh_frame",
            ".llvm_addrsig", ".symtab"])
        self.assertEqual(result["sections"][0], {"name": ".strtab", "bytes": 133, "kind": ""})
        self.assertEqual(result["sections"][6], {"name": ".comment", "bytes": 110, "kind": ""})
        self.assertEqual((result["text_bytes"], result["initialized_data_bytes"], result["bss_bytes"]),
                         (27, 65, 4))

    def test_mach_o_and_coff_sections_have_exact_sizes_and_types(self):
        for output, names, sizes in (
            (MACH_O, ["__text", "__data", "__const", "__common", "__compact_unwind"], (36, 41, 4)),
            (COFF, [".text", ".data", ".bss", ".rdata", ".debug$S", ".llvm_addrsig"], (22, 169, 4)),
        ):
            for newline in ("\n", "\r\n"):
                with self.subTest(format=names[0], newline=repr(newline)):
                    result = self.parse(output.replace("\n", newline))
                    self.assertEqual([row["name"] for row in result["sections"]], names)
                    self.assertEqual((result["text_bytes"], result["initialized_data_bytes"], result["bss_bytes"]), sizes)
                    self.assertTrue(all("\n" not in row["kind"] for row in result["sections"]))

    def test_elf_null_row_does_not_hide_following_initialized_section(self):
        result = self.parse("Idx Name Size VMA Type\n"
                            " 0                 00000000 0000000000000000 \n"
                            " 1 .interp         0000001c 0000000000000318 DATA\n")
        self.assertEqual(result["sections"], [{"name": ".interp", "bytes": 28, "kind": "DATA"}])
        self.assertEqual(result["initialized_data_bytes"], 28)

    def test_missing_section_rows_fail(self):
        with self.assertRaisesRegex(RuntimeError, "no final section sizes"):
            self.parse("sample.o: file format elf64-x86-64\nSections:\nIdx Name Size VMA Type\n")


if __name__ == "__main__":
    unittest.main()
