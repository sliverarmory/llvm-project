"""Run each custom transform through LLVM 23's named new-PM opt pipeline."""

import argparse
import subprocess
import sys
from pathlib import Path

from test_transforms import CASES, check_combined_effect, check_effect, expected


SEED = "00112233445566778899aabbccddeeff"
PASSES = {
    "sobf": "obf-string",
    "sub": "obf-sub",
    "split": "obf-split",
    "bcf": "obf-bcf",
    "fla": "obf-fla",
}
COMBINED = ",".join(PASSES.values())


def run(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    args = parser.parse_args()

    clang = [str(args.clang.resolve())]
    if args.sysroot:
        clang.extend(("-isysroot", str(args.sysroot.resolve())))
    elif sys.platform == "darwin":
        clang.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).strip()))
    opt = str(args.opt.resolve())
    source = Path(__file__).with_name("smoke.c").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    for level in ("O0", "O2"):
        baseline_path = work_dir / f"{level}-baseline.ll"
        disable_optnone = ("-Xclang", "-disable-O0-optnone") if level == "O0" else ()
        run([
            *clang, f"-{level}", *disable_optnone,
            "-fno-discard-value-names", "-S", "-emit-llvm", str(source),
            "-o", str(baseline_path),
        ])
        baseline = baseline_path.read_text(encoding="utf-8")

        for name, pipeline in (*PASSES.items(), ("combined", COMBINED)):
            output = work_dir / f"{level}-{name}.ll"
            bcf_options = ("-bcf_prob=100",) if name in ("bcf", "combined") else ()
            run([
                opt, f"-obf-test-seed={SEED}", *bcf_options,
                f"-passes={pipeline},verify",
                "-S", str(baseline_path), "-o", str(output),
            ])
            run([opt, "-passes=verify", "-disable-output", str(output)])
            ir = output.read_text(encoding="utf-8")
            if name == "combined":
                check_combined_effect(ir, baseline)
            else:
                check_effect(name, ir, baseline)

            executable = work_dir / f"{level}-{name}"
            if sys.platform == "win32":
                executable = executable.with_suffix(".exe")
            run([*clang, "-x", "ir", "-O0", str(output), "-o", str(executable)])
            for a, b in CASES:
                assert run([str(executable), str(a), str(b)], timeout=10) == expected(a, b), (
                    f"{level}/{name} changed program behavior for {a}, {b}"
                )
            print(f"[{level}] opt -passes={pipeline} verifies and runs", flush=True)


if __name__ == "__main__":
    main()
