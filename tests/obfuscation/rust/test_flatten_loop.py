#!/usr/bin/env python3
"""Catch wrong entry-state dispatch in optimized Rust loops."""

import argparse
import platform
import re
import subprocess
import sys
from pathlib import Path


SOURCE = Path(__file__).with_name("flatten_loop.rs")
SEED = "00112233445566778899aabbccddeeff"
MASK = (1 << 64) - 1
CASES = (("O0-off", 0, 1, "off"),
         ("O2-off", 2, 1, "off"),
         ("O2-local-thin", 2, 4, "false"),
         ("O2-thin", 2, 4, "thin"),
         ("O2-fat", 2, 4, "fat"))


def run(command, *, timeout=240):
    result = subprocess.run([str(part) for part in command], capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"command exited {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def expected_output():
    values = []
    for initial in (1, 2, MASK):
        value = initial
        for index in range(31):
            rotation = index % 29 + 1
            value = ((value << rotation) | (value >> (64 - rotation))) & MASK
            value = (value + 0x5A17) & MASK
            value = value * (7 if value & 1 == 0 else 9) & MASK
        values.append(str(value))
    return "\n".join(values) + "\n"


def machine_instructions(objdump, executable):
    symbol = "_flatten_loop" if platform.system() == "Darwin" else "flatten_loop"
    output = run([objdump, f"--disassemble-symbols={symbol}",
                  "--no-show-raw-insn", executable]).stdout
    if f"<{symbol}>:" not in output:
        raise AssertionError(f"final executable lacks {symbol}")
    instructions = re.findall(r"(?m)^\s*[0-9a-f]+:\s+([a-z][a-z0-9_.]*)\b", output)
    if not instructions:
        raise AssertionError(f"no final instructions for {symbol}")
    return instructions


def check_case(rustc, opt, objdump, work, label, level, cgus, lto):
    executables = {}
    ir_paths = {}
    for variant in ("ordinary", "flattened"):
        stem = work / f"{label}-{variant}"
        executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
        command = [rustc, "--edition=2024", "-C", f"opt-level={level}",
                   "-C", f"codegen-units={cgus}", "-C", f"lto={lto}",
                   "-C", "panic=abort"]
        if sys.platform == "win32":
            command += ["-C", "link-arg=/EXPORT:flatten_loop"]
        if variant == "flattened":
            llvm = ("-rust-obf-pipeline=obf-fla -rust-obf-prelink-only "
                    f"-obf-only-functions=flatten_loop -obf-test-seed={SEED}")
            command += ["-C", f"llvm-args={llvm}"]
        if cgus == 1 and lto == "off":
            ir_path = stem.with_suffix(".ll")
            command += [f"--emit=llvm-ir={ir_path},link={executable}"]
            ir_paths[variant] = ir_path
        else:
            # --emit=llvm-ir together with -o would reset multi-CGU builds
            # to one codegen unit. Save the bitcode from this link invocation
            # instead, and remove only this variant's prior saved outputs so
            # a rerun cannot accidentally verify stale IR.
            for stale in work.glob(f"{stem.name}.*.rcgu.bc"):
                stale.unlink()
            command += ["-C", "save-temps=yes", "-o", executable]
        run([*command, SOURCE], timeout=600)
        if cgus != 1 or lto != "off":
            bitcode = sorted(work.glob(f"{stem.name}.*.rcgu.bc"))
            if not bitcode:
                raise AssertionError(f"{label}/{variant}: rustc saved no emitted bitcode")
            for path in bitcode:
                run([opt, "-passes=verify", "-disable-output", path])
        output = run([executable], timeout=30).stdout
        if output != expected_output():
            raise AssertionError(f"{label}/{variant}: wrong loop result: {output!r}")
        executables[variant] = executable
    if ir_paths:
        for ir_path in ir_paths.values():
            run([opt, "-passes=verify", "-disable-output", ir_path])
        if "switchVar" not in ir_paths["flattened"].read_text():
            raise AssertionError(f"{label}: flattening dispatcher missing from IR")
    ordinary = machine_instructions(objdump, executables["ordinary"])
    flattened = machine_instructions(objdump, executables["flattened"])
    if ordinary == flattened:
        raise AssertionError(f"{label}: final loop instructions unchanged")
    print(f"PASS {label}: exact loop output, final effect, IR verified=True",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        check_case(args.rustc.resolve(), args.opt.resolve(),
                   args.objdump.resolve(), work, *case)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
