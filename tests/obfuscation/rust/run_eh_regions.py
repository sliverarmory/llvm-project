#!/usr/bin/env python3
"""Check BCF/flattening in Rust normal regions adjoining unwind cleanup.

Use the pinned custom Rust 1.99 stage1 compiler after rebuilding LLVM and
relinking rustc. The fixture checks normal results, explicit panic, checked
overflow, and exact Drop count/order under both panic strategies.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


SOURCE = Path(__file__).with_name("eh_regions.rs")
SEED = "00112233445566778899aabbccddeeff"
EXPECTED_UNWIND = (
    "6:0:29:2:33:3\n"
    "7:0:46:2:33:5\n"
    "6:1:panic:2:33:3\n"
    "2147483647:2:panic:2:33:5\n"
)
EXPECTED_ABORT = "6:0:29:2:33:3\n7:0:46:2:33:5\n"
VARIANTS = {
    "plain": (),
    "bcf": ("obf-bcf",),
    "fla": ("obf-fla",),
    "combined": ("obf-bcf", "obf-fla"),
}


def run(argv: list[str], *, check: bool = True, timeout: int = 600):
    result = subprocess.run(argv, text=True, capture_output=True,
                            timeout=timeout)
    if check and result.returncode:
        raise AssertionError(
            f"command exited {result.returncode}: {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def function_body(ir: str) -> str:
    match = re.search(r"(?m)^define\b[^\n]*@eh_probe\([^\n]*\{\n", ir)
    if not match:
        raise AssertionError("emitted IR lacks eh_probe definition")
    end = ir.find("\n}", match.end())
    if end < 0:
        raise AssertionError("eh_probe definition is incomplete")
    return ir[match.end():end]


def unwind_targets(body: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\binvoke\b[^\n]*\n\s*to label %\S+ "
                            r"unwind label %(\S+)", body))


def opcode_count(body: str, opcode: str) -> int:
    return len(re.findall(rf"(?m)^\s+(?:%[^\s=]+\s*=\s*)?{opcode}\b",
                          body))


def check_ir(label: str, body: str, baseline: str, panic: str,
             variant: str) -> None:
    if panic == "unwind":
        expected_pad = "cleanuppad" if sys.platform == "win32" else "landingpad"
        if opcode_count(baseline, expected_pad) == 0 or not unwind_targets(baseline):
            raise AssertionError(f"{label}: baseline misses unwind EH")
        for opcode in ("landingpad", "cleanuppad", "catchpad", "catchswitch",
                       "catchret", "cleanupret", "resume"):
            if opcode_count(body, opcode) != opcode_count(baseline, opcode):
                raise AssertionError(f"{label}: {opcode} count changed")
        if unwind_targets(body) != unwind_targets(baseline):
            raise AssertionError(f"{label}: invoke unwind destinations changed")
    elif any(opcode_count(body, pad) for pad in
             ("landingpad", "cleanuppad", "catchpad")) or unwind_targets(body):
        raise AssertionError(f"{label}: panic=abort unexpectedly gained EH")

    if variant in ("bcf", "combined"):
        if "load volatile i32" not in body or "urem i32" not in body:
            raise AssertionError(f"{label}: BCF did not rewrite a normal block")
    if variant in ("fla", "combined"):
        if "switch i32" not in body:
            raise AssertionError(f"{label}: flattening dispatcher missing")
        if panic == "unwind":
            if "eh.dispatch" not in body or "store volatile i32" not in body:
                raise AssertionError(
                    f"{label}: EH normal-region dispatcher state missing")


def compile_case(rustc: Path, opt: Path, work_dir: Path, level: int,
                 panic: str, variant: str, baseline: str | None) -> str:
    label = f"O{level}-{panic}-{variant}"
    stem = work_dir / label
    ir_path = stem.with_suffix(".ll")
    binary = stem.with_suffix(".exe") if sys.platform == "win32" else stem
    args = [
        str(rustc), "--edition=2024", "-C", f"opt-level={level}",
        "-C", f"panic={panic}", "-C", "overflow-checks=yes",
        "-C", "codegen-units=1", "-C", "lto=off",
    ]
    passes = VARIANTS[variant]
    if passes:
        llvm_args = [f"-rust-obf-pipeline={','.join(passes)}",
                     "-obf-only-functions=eh_probe", f"-obf-test-seed={SEED}"]
        if "obf-bcf" in passes:
            llvm_args.append("-bcf_prob=100")
        args.extend(("-C", f"llvm-args={' '.join(llvm_args)}"))

    run([*args, "--emit=llvm-ir", str(SOURCE), "-o", str(ir_path)])
    ir = ir_path.read_text(encoding="utf-8")
    body = function_body(ir)
    if baseline is not None:
        check_ir(label, body, baseline, panic, variant)
    run([str(opt), "-passes=verify", "-disable-output", str(ir_path)])

    run([*args, str(SOURCE), "-o", str(binary)])
    actual = run([str(binary)], timeout=30).stdout
    expected = EXPECTED_UNWIND if panic == "unwind" else EXPECTED_ABORT
    if actual != expected:
        raise AssertionError(
            f"{label}: runtime mismatch:\n{actual}\nexpected:\n{expected}"
        )
    if panic == "abort" and variant == "combined":
        failed = run([str(binary), "--uncaught-panic"], check=False,
                     timeout=30)
        if failed.returncode == 0:
            raise AssertionError(f"{label}: uncaught panic did not abort")
    print(f"PASS {label}: IR verified and Drop/panic/overflow output matched",
          flush=True)
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    rustc = args.rustc.resolve()
    opt = args.opt.resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    version = run([str(rustc), "-vV"], timeout=30).stdout
    if not re.search(r"^release: 1\.99\.", version, re.MULTILINE) or not re.search(
            r"^LLVM version: 23\.", version, re.MULTILINE):
        raise AssertionError("expected pinned custom Rust 1.99 / LLVM 23")

    for level in (0, 2):
        for panic in ("unwind", "abort"):
            baseline = compile_case(rustc, opt, work_dir, level, panic,
                                    "plain", None)
            variants = ("bcf", "fla", "combined") if panic == "unwind" else (
                "combined",)
            for variant in variants:
                compile_case(rustc, opt, work_dir, level, panic, variant,
                             baseline)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
