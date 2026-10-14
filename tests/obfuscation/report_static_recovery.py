"""Recover selected fixture data statically from final encoded executables.

The string attack knows only a prefix and length. The constant attack groups
adjacent 32-bit rows by their XOR difference; it does not consume LLVM IR,
source constants, decoder keys, debug information, or the compiler seed.
Expected fixture values are used only afterward to validate the candidates.
"""

import argparse
from collections import defaultdict
import hashlib
import json
import platform
from pathlib import Path
import sys

from binary_evidence import checked, native_flags, readonly_data_sections, sibling_tool


SOURCE = Path(__file__).with_name("recovery.c")
EXPECTED_STRING = b"known-prefix:recoverable-string-payload\0"
KNOWN_PREFIX = b"known-prefix:"
EXPECTED_CONSTANT = 0x6B7D4A91
FIXTURE_SEED = "00112233445566778899aabbccddeeff"
CASES = (0, 1, 2, 17, 0x80000000, 0xFFFFFFFF)


def recover_rolling_masks(binary, prefix, size):
    if len(prefix) < 3 or size < len(prefix):
        raise ValueError("need at least three known bytes and a compatible size")
    matches = []
    for offset in range(len(binary) - size + 1):
        key = binary[offset] ^ prefix[0]
        step = ((binary[offset + 1] ^ prefix[1]) - key) & 255
        if step % 2 == 0:
            continue
        if any(binary[offset + index] ^ ((key + index * step) & 255) != value
               for index, value in enumerate(prefix[2:], 2)):
            continue
        decoded = bytes(binary[offset + index] ^ ((key + index * step) & 255)
                        for index in range(size))
        if decoded[-1] != 0 or any(byte < 32 or byte > 126 for byte in decoded[:-1]):
            continue
        matches.append({"file_offset": offset, "key": key, "step": step,
                        "decoded_hex": decoded.hex(), "decoded_text": decoded[:-1].decode("ascii")})
    return matches


def recover_two_shares(sections):
    buckets = defaultdict(list)
    for section in sections:
        data, address = section["data"], section["address"]
        if address is None:
            continue
        for offset in range((-address) % 4, len(data) - 7, 4):
            first = int.from_bytes(data[offset:offset + 4], "little")
            second = int.from_bytes(data[offset + 4:offset + 8], "little")
            delta = first ^ second
            if delta:
                buckets[delta].append({"section": section["name"], "address": address + offset,
                                       "rows": [first, second]})
    candidates = []
    noisy_buckets = 0
    for delta, pairs in buckets.items():
        # Repeated padding/instruction-looking data is not a useful table
        # signature. Bound work on arbitrary executables, not just fixtures.
        if len(pairs) > 64:
            noisy_buckets += 1
            continue
        for index, left in enumerate(pairs):
            for right in pairs[index + 1:]:
                if abs(left["address"] - right["address"]) < 8:
                    continue
                value = left["rows"][0] ^ right["rows"][0]
                if value == 0:
                    continue
                candidates.append({"value": value, "value_hex": f"0x{value:08x}",
                                   "delta_hex": f"0x{delta:08x}", "left": left, "right": right})
    return candidates, noisy_buckets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--objdump", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("timeout must be positive")
    clang = args.clang.resolve()
    objdump = args.objdump.resolve() if args.objdump else sibling_tool(clang, "llvm-objdump")
    directory = args.work_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    common = [str(clang), *native_flags(args.sysroot), "-std=c11", "-fno-builtin"]
    reports = []
    for level in ("O0", "O2"):
        binaries = {}
        commands = {}
        for variant, flags in (
            ("baseline", ()),
            ("encoded", ("-sobf", "-sobf-only-globals=recovery_text", "-constenc",
                         "-constenc-values=i32:0x6b7d4a91", "-obf-only-functions=recovery_probe",
                         f"-obf-test-seed={FIXTURE_SEED}")),
        ):
            executable = directory / f"recovery-{level}-{variant}{suffix}"
            commands[variant] = [*common, f"-{level}", *(part for flag in flags for part in ("-mllvm", flag)),
                                 str(SOURCE), "-o", str(executable)]
            checked(commands[variant], args.timeout)
            binaries[variant] = executable
        outputs = []
        for value in CASES:
            expected = EXPECTED_STRING[:-1].decode() + "\n" + str((value + EXPECTED_CONSTANT) & 0xFFFFFFFF) + "\n"
            for variant, binary in binaries.items():
                actual = checked([str(binary), str(value)], args.timeout)
                if actual != expected:
                    raise RuntimeError(f"{level}/{variant} differs for {value}: {actual!r}")
            outputs.append({"input": value, "output": expected})
        baseline, encoded = (binaries[key].read_bytes() for key in ("baseline", "encoded"))
        assert EXPECTED_STRING in baseline, "baseline string fixture missing"
        assert EXPECTED_STRING not in encoded, "encoded binary exposes original plaintext"
        assert EXPECTED_CONSTANT.to_bytes(4, "little") not in encoded, "encoded binary contains selected immediate bytes"
        recovered_strings = recover_rolling_masks(encoded, KNOWN_PREFIX, len(EXPECTED_STRING))
        assert any(bytes.fromhex(item["decoded_hex"]) == EXPECTED_STRING for item in recovered_strings), "known-prefix recovery failed"
        sections = readonly_data_sections(objdump, binaries["encoded"])
        constants, noisy = recover_two_shares(sections)
        matching = [item for item in constants if item["value"] == EXPECTED_CONSTANT]
        assert matching, "two-share static pattern recovery failed"
        reports.append({"optimization": level, "binary": str(binaries["encoded"]),
                        "compile_commands": commands,
                        "binary_sha256": hashlib.sha256(encoded).hexdigest(),
                        "runtime_equivalence": outputs,
                        "rolling_mask": {"known_prefix": KNOWN_PREFIX.decode(),
                                         "known_length": len(EXPECTED_STRING), "candidates": recovered_strings},
                        "two_share": {"candidate_count": len(constants), "ignored_noisy_buckets": noisy,
                                      "validated_candidates": matching,
                                      "data_sections": [section["name"] for section in sections]},
                        "original_string_absent": True, "original_immediate_bytes_absent": True})
        print(f"{level}: exact runtime equivalence for {len(CASES)} inputs; recovered string and i32 constant from final binary", flush=True)
    report = {"schema_version": 1, "host": platform.platform(), "experiments": reports,
              "compiler": {"path": str(clang), "version": checked([str(clang), "--version"]).splitlines()[0]},
              "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              "limitations": ["Known-prefix string experiment assumes its encoded allocation length.",
                              "Two-share scanning targets adjacent 32-bit rows in little-endian read-only sections.",
                              "Pattern matches are candidates; fixture expected values validate them after recovery.",
                              "The recovery algorithms receive no IR, debug info, compiler seed, or source constant.",
                              "These encodings obstruct casual inspection; they do not provide secrecy."]}
    path = directory / "static-recovery.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
