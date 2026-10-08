#!/usr/bin/env python3
"""Build and run the stage-aware pipeline fixture with a pinned custom rustc.

The exact function filter keeps LTO's sysroot modules outside the selected
transformation. This runner checks pass invocation and effect in the printed
IR, then checks the linked program's output against fixed expected values.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(__file__).with_name("pipeline.rs")
SEEDS = (0, 1, 0x1234_5678_9ABC_DEF0, (1 << 64) - 1)
IR_BEFORE = "; *** IR Dump Before SubstitutionPass on pipeline_probe ***"
IR_AFTER = "; *** IR Dump After SubstitutionPass on pipeline_probe ***"
SEED = "00112233445566778899aabbccddeeff"
CASES = (
    ("O0", 0, "off"),
    ("O2", 2, "off"),
    ("O2-thin", 2, "thin"),
    ("O2-fat", 2, "fat"),
    ("O0-thin", 0, "thin"),
    ("O0-fat", 0, "fat"),
)


def expected_output() -> str:
    mask = (1 << 64) - 1
    answers = []
    for seed in SEEDS:
        value = seed ^ 0x6A09_E667_F3BC_C909
        for round_number in range(1, 97):
            rotate = round_number % 31 + 1
            value = ((value << rotate) | (value >> (64 - rotate))) & mask
            value = (value * 0x9E37_79B9_7F4A_7C15) & mask
            if (value ^ round_number) & 1 == 0:
                value = (value + round_number * 17) & mask
            else:
                value = (value - round_number * 23) & mask
        answers.append(f"{value:016x}")
    return "\n".join(answers) + "\n"


def run(argv: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"command exited {result.returncode}: {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def check_toolchain(rustc: Path) -> None:
    version = run([str(rustc), "-vV"], timeout=30).stdout
    if not re.search(r"^release: 1\.99\.", version, re.MULTILINE):
        raise AssertionError(f"expected pinned Rust 1.99 source, got:\n{version}")
    if not re.search(r"^LLVM version: 23\.", version, re.MULTILINE):
        raise AssertionError(f"expected this fork's LLVM 23, got:\n{version}")


def check_case(rustc: Path, work_dir: Path, label: str, level: int,
               lto: str) -> None:
    binary = work_dir / label
    options = [
        "-rust-obf-pipeline=obf-sub",
        "-obf-only-functions=pipeline_probe",
        f"-obf-test-seed={SEED}",
        "-print-before=obf-sub",
        "-print-after=obf-sub",
        "-filter-print-funcs=pipeline_probe",
    ]
    command = [
        str(rustc), "--edition=2024", "-C", f"opt-level={level}",
        "-C", "panic=abort", "-C", "codegen-units=1",
        "-C", f"lto={lto}", "-C", f"llvm-args={' '.join(options)}",
        str(SOURCE), "-o", str(binary),
    ]
    result = run(command)
    (work_dir / f"{label}.stderr").write_text(result.stderr, encoding="utf-8")
    before_count = result.stderr.count(IR_BEFORE)
    after_count = result.stderr.count(IR_AFTER)
    if (before_count, after_count) != (1, 1):
        raise AssertionError(
            f"{label}: expected one pass on pipeline_probe, got "
            f"before={before_count}, after={after_count}"
        )
    before_start = result.stderr.index(IR_BEFORE) + len(IR_BEFORE)
    after_start = result.stderr.index(IR_AFTER)
    before_ir = result.stderr[before_start:after_start]
    after_ir = result.stderr[after_start + len(IR_AFTER):]
    # Substitution freezes reused operands; counting new freeze instructions
    # demonstrates a real rewrite at both O0 (numbered SSA) and optimized IR.
    if after_ir.count(" = freeze ") <= before_ir.count(" = freeze "):
        raise AssertionError(f"{label}: pass ran but did not rewrite the probe")
    output = run([str(binary)], timeout=30).stdout
    expected = expected_output()
    if output != expected:
        raise AssertionError(
            f"{label}: runtime output changed:\n{output}\nexpected:\n{expected}"
        )
    print(f"PASS rustc {label}: one transformed pass and exact runtime output")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument(
        "--work-dir", type=Path, default=ROOT / "build-llvm-project" / "rust-m1",
    )
    args = parser.parse_args()
    rustc = args.rustc.resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    check_toolchain(rustc)
    for case in CASES:
        check_case(rustc, work_dir, *case)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
