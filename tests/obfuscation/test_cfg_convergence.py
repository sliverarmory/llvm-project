"""Verify CFG obfuscation leaves convergent IR intact and transforms a peer."""

import argparse
import re
import subprocess
from pathlib import Path


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def function_body(ir, name):
    match = re.search(
        rf"(?ms)^define [^\n]*@{re.escape(name)}\([^\n]*\) [^\n]*\{{\n(.*?)^\}}",
        ir,
    )
    if not match:
        raise AssertionError(f"Missing {name} in emitted IR")
    return match.group(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    fixture = Path(__file__).with_name("cfg_convergence.ll").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    clang = str(args.clang.resolve())
    opt = str(args.opt.resolve())

    run([opt, "-passes=verify", "-disable-output", str(fixture)])
    variants = {
        "fla": ("-fla",),
        "split": ("-split", "-split_num=10"),
    }
    for level in ("O0", "O2"):
        for name, flags in variants.items():
            output = work_dir / f"cfg-convergence-{level}-{name}.ll"
            print(f"[{level}] compile {name} convergence fixture", flush=True)
            options = [item for flag in flags for item in ("-mllvm", flag)]
            run([
                clang, "-x", "ir", f"-{level}", *options, "-S", "-emit-llvm",
                str(fixture), "-o", str(output),
            ])
            run([opt, "-passes=verify", "-disable-output", str(output)])
            ir = output.read_text(encoding="utf-8")
            controlled = function_body(ir, "controlled_loop")
            uncontrolled = function_body(ir, "uncontrolled_target")
            attribute_only = function_body(ir, "convergent_attribute_only")
            ordinary = function_body(ir, "ordinary_target")
            eh_cleanup = function_body(ir, "convergent_eh_cleanup")
            assert "@llvm.experimental.convergence.entry()" in controlled
            assert "@llvm.experimental.convergence.loop()" in controlled
            assert "@convergent_op" in uncontrolled
            effect = "switch i32" if name == "fla" else ".split"
            for protected in (controlled, uncontrolled, attribute_only):
                assert effect not in protected, f"{name} rewrote convergent IR"
            if name == "split":
                assert effect not in eh_cleanup, (
                    "split rewrote normal blocks despite a convergent "
                    "call in protected EH cleanup")
            assert effect in ordinary, f"{name} skipped the ordinary peer"
            print(f"[{level}] {name} preserves convergence and transforms peer", flush=True)


if __name__ == "__main__":
    main()
