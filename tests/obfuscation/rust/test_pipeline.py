#!/usr/bin/env python3
"""Check the opt-in Rust PassBuilder pipeline and Clang callback isolation.

Run after rebuilding this fork's opt and clang:
  python3 tests/obfuscation/rust/test_pipeline.py \
    --opt build-llvm-project/bin/opt --clang build-llvm-project/bin/clang
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ALL = (
    "obf-string",
    "obf-split",
    "obf-bcf",
    "obf-fla",
    "obf-sub",
    "obf-const",
    "obf-global-access",
)
LATE = ALL[1:]
PASS_NAMES = re.compile(
    r"(?<![\w-])obf-(?:string|split|bcf|fla|sub|const|global-access)(?![\w-])"
)


def run(*argv: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True, timeout=120)
    if (result.returncode == 0) != expect_success:
        raise AssertionError(
            f"command exited {result.returncode}: {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def selected_passes(text: str) -> tuple[str, ...]:
    return tuple(PASS_NAMES.findall(text))


def check_opt(opt: Path) -> None:
    stages = (
        ("default<O0>", ALL),
        ("default<O2>", ALL),
        ("default<O3>", ALL),
        ("thinlto-pre-link<O0>", ALL),
        ("thinlto-pre-link<O2>", ALL[:1]),
        ("lto-pre-link<O2>", ALL[:1]),
        ("thinlto<O0>", ()),
        ("thinlto<O2>", LATE),
        ("lto<O0>", ()),
        ("lto<O2>", LATE),
    )
    for stage, expected in stages:
        result = run(
            str(opt),
            "-rust-obf-pipeline=all",
            f"-passes={stage}",
            "-print-pipeline-passes=text",
            "-disable-output",
            os.devnull,
        )
        actual = selected_passes(result.stdout)
        if actual != expected:
            raise AssertionError(f"{stage}: expected {expected}, got {actual}")
        print(f"PASS opt {stage}: {', '.join(actual) or 'no postlink rerun'}")

    # Cargo selects packages before rustc runs. In this mode the selected
    # package is rewritten before LTO merges modules, and its link-time
    # pipeline cannot rewrite an unselected dependency or sysroot module.
    prelink_stages = (
        ("default<O0>", ALL),
        ("default<O2>", ALL),
        ("thinlto-pre-link<O0>", ALL),
        ("thinlto-pre-link<O2>", ALL),
        ("lto-pre-link<O2>", ALL),
        ("thinlto<O0>", ()),
        ("thinlto<O2>", ()),
        ("lto<O0>", ()),
        ("lto<O2>", ()),
    )
    for stage, expected in prelink_stages:
        result = run(
            str(opt), "-rust-obf-pipeline=all", "-rust-obf-prelink-only",
            f"-passes={stage}", "-print-pipeline-passes=text",
            "-disable-output", os.devnull,
        )
        actual = selected_passes(result.stdout)
        if actual != expected:
            raise AssertionError(
                f"prelink-only {stage}: expected {expected}, got {actual}")
        print(f"PASS opt prelink-only {stage}: "
              f"{', '.join(actual) or 'no postlink rerun'}")

    for stage in ("default<O0>", "default<O2>", "thinlto<O2>", "lto<O2>"):
        result = run(
            str(opt), f"-passes={stage}", "-print-pipeline-passes=text",
            "-disable-output", os.devnull,
        )
        if selected_passes(result.stdout):
            raise AssertionError(f"{stage}: Rust passes inserted without opt-in")

    result = run(
        str(opt),
        "-rust-obf-pipeline=obf-sub,obf-string,obf-split,obf-sub",
        "-passes=default<O2>",
        "-print-pipeline-passes=text",
        "-disable-output", os.devnull,
    )
    expected_subset = ("obf-string", "obf-split", "obf-sub")
    if selected_passes(result.stdout) != expected_subset:
        raise AssertionError("selection order or deduplication changed")
    print("PASS opt unordered selection: canonical order, one insertion each")

    result = run(
        str(opt), "-rust-obf-pipeline=not-a-pass", "-passes=default<O2>",
        "-disable-output", os.devnull, expect_success=False,
    )
    if "Cannot find option named 'not-a-pass'" not in result.stderr:
        raise AssertionError(f"unknown pass lacked a clear error: {result.stderr}")
    print("PASS opt unknown selection: clear error")


def check_clang(clang: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rust-obf-clang-") as directory:
        source = Path(directory) / "callbacks.c"
        source.write_text(
            "__attribute__((noinline)) int calc(int x) {\n"
            "  if (x & 1) return x * 3 + 9;\n"
            "  return x * 7 - 4;\n"
            "}\n",
            encoding="utf-8",
        )
        common = (
            str(clang), "-O2", "-S", "-emit-llvm", "-o", os.devnull,
            str(source), "-mllvm", "-split", "-mllvm",
            "-print-pipeline-passes=tree",
        )
        legacy = run(*common)
        if "ConditionalObfuscationPass" not in legacy.stdout:
            raise AssertionError("Clang legacy callback was not registered")
        combined = run(*common, "-mllvm", "-rust-obf-pipeline=obf-split")
        if selected_passes(combined.stdout) != ("obf-split",):
            raise AssertionError("Clang inserted Rust pass more than once")
        if "ConditionalObfuscationPass" in combined.stdout:
            raise AssertionError("Clang also inserted its legacy callbacks")
        print("PASS Clang legacy flag plus Rust opt-in: one pass, no duplicate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--clang", type=Path, required=True)
    args = parser.parse_args()
    check_opt(args.opt.resolve())
    check_clang(args.clang.resolve())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
