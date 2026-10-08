#!/usr/bin/env python3
"""Verify normal-region control-flow rewrites with Itanium and MSVC EH IR.

The Windows MSVC target is an IR/verifier check when this suite runs on macOS
or Linux. Native Rust unwind and Drop behavior is covered by run_eh_regions.py.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


SOURCE = Path(__file__).with_name("eh_regions.cpp")
VARIANTS = {
    "plain": (),
    "bcf": ("obf-bcf",),
    "fla": ("obf-fla",),
    "combined": ("obf-bcf", "obf-fla"),
}


def run(argv: list[str]) -> None:
    result = subprocess.run(argv, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise AssertionError(
            f"command exited {result.returncode}: {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def body(ir: str) -> str:
    match = re.search(r"(?m)^define\b[^\n]*guarded_eh[^\n]*\{\n", ir)
    if not match:
        raise AssertionError("guarded_eh definition missing")
    end = ir.find("\n}", match.end())
    if end < 0:
        raise AssertionError("guarded_eh definition incomplete")
    return ir[match.end():end]


def unwind_targets(ir_body: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\binvoke\b[^\n]*\n\s*to label %\S+ "
                            r"unwind label %(\S+)", ir_body))


def named_block(ir_body: str, name: str) -> str:
    match = re.search(rf"(?m)^{re.escape(name)}:[^\n]*\n", ir_body)
    if not match:
        raise AssertionError(f"{name} block missing")
    end = re.search(r"(?m)^[^\s;][^:\n]*:", ir_body[match.end():])
    return ir_body[match.end():match.end() + end.start()] if end else ir_body[
        match.end():]


def check_effect(label: str, ir: str, base: str, variant: str,
                 pad: str) -> None:
    target = body(ir)
    baseline = body(base)
    if pad not in baseline or not unwind_targets(baseline):
        raise AssertionError(f"{label}: baseline misses {pad} and invoke")
    if target.count(pad) != baseline.count(pad):
        raise AssertionError(f"{label}: {pad} count changed")
    if unwind_targets(target) != unwind_targets(baseline):
        raise AssertionError(f"{label}: invoke unwind destinations changed")
    for opcode in ("catchswitch", "catchret", "cleanupret", "resume"):
        if len(re.findall(rf"(?m)^\s+{opcode}\b", target)) != len(
                re.findall(rf"(?m)^\s+{opcode}\b", baseline)):
            raise AssertionError(f"{label}: {opcode} count changed")
    if pad == "landingpad":
        shared_join = "return" if label.startswith("O0-") else "cleanup"
        protected = named_block(target, shared_join)
        if "urem i32" in protected or "switch i32" in protected:
            raise AssertionError(f"{label}: shared cleanup join was transformed")
    if variant in ("bcf", "combined"):
        if "load volatile i32" not in target or "urem i32" not in target:
            raise AssertionError(f"{label}: BCF normal-region rewrite missing")
        if ".obf.bcf.x" not in ir or ".obf.bcf.y" not in ir:
            raise AssertionError(f"{label}: BCF private predicate state missing")
    if variant in ("fla", "combined"):
        if "eh.dispatch" not in target or "switch i32" not in target:
            raise AssertionError(f"{label}: flattening EH dispatcher missing")
        if "store volatile i32" not in target:
            raise AssertionError(f"{label}: flattening state is not volatile")


def compile_ir(clang: Path, opt: Path, work_dir: Path, level: int,
               target_name: str, variant: str) -> str:
    label = f"O{level}-{target_name}-{variant}"
    path = work_dir / f"{label}.ll"
    command = [str(clang), "-x", "c++", "-std=c++17", f"-O{level}",
               "-fexceptions", "-fcxx-exceptions"]
    if target_name == "windows-msvc":
        command.append("--target=x86_64-pc-windows-msvc")
    passes = VARIANTS[variant]
    if passes:
        command.extend(("-mllvm", f"-rust-obf-pipeline={','.join(passes)}"))
        if "obf-bcf" in passes:
            command.extend(("-mllvm", "-bcf_prob=100"))
        command.extend(("-mllvm", "-obf-test-seed="
                        "00112233445566778899aabbccddeeff"))
    command.extend(("-S", "-emit-llvm", str(SOURCE), "-o", str(path)))
    run(command)
    run([str(opt), "-passes=verify", "-disable-output", str(path)])
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    clang = args.clang.resolve()
    opt = args.opt.resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    for level in (0, 2):
        for target_name, pad in (("native", "landingpad"),
                                 ("windows-msvc", "catchpad")):
            baseline = compile_ir(clang, opt, work_dir, level,
                                  target_name, "plain")
            for variant in ("bcf", "fla", "combined"):
                label = f"O{level}-{target_name}-{variant}"
                ir = compile_ir(clang, opt, work_dir, level,
                                target_name, variant)
                check_effect(label, ir, baseline, variant, pad)
                print(f"PASS {label}: transformed normal CFG, preserved EH",
                      flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
