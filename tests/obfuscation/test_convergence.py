"""Check that BCF preserves LLVM convergence-control IR at O0 and O2."""

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
    match = re.search(rf"(?ms)^define [^\n]*@{re.escape(name)}\([^\n]*\) [^\n]*\{{\n(.*?)^\}}", ir)
    if not match:
        raise AssertionError(f"Missing {name} in emitted IR")
    return match.group(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    fixture = Path(__file__).with_name("convergence.ll").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    clang = str(args.clang.resolve())
    opt = str(args.opt.resolve())

    for level in ("O0", "O2"):
        output = work_dir / f"convergence-{level}.ll"
        print(f"[{level}] compile BCF convergence fixture", flush=True)
        run([
            clang, "-x", "ir", f"-{level}", "-mllvm", "-bcf", "-mllvm",
            "-bcf_prob=100", "-S", "-emit-llvm", str(fixture), "-o",
            str(output),
        ])
        run([opt, "-passes=verify", "-disable-output", str(output)])
        ir = output.read_text(encoding="utf-8")
        protected = function_body(ir, "convergent_target")
        ordinary = function_body(ir, "ordinary_target")
        assert "@llvm.experimental.convergence.entry()" in protected
        assert '"convergencectrl"' in protected
        assert "alteredBB" not in protected, "BCF rewrote convergence-control IR"
        assert "alteredBB" in ordinary and "urem i32" in ordinary, (
            "BCF failed to transform the ordinary peer function"
        )
        print(f"[{level}] BCF preserves convergence control and transforms peer", flush=True)


if __name__ == "__main__":
    main()
