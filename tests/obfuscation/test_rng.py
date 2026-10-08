"""Check reproducible obfuscation and strict test-seed parsing on LLVM 23."""

import argparse
import subprocess
import sys
from pathlib import Path


SEED_A = "00112233445566778899aabbccddeeff"
SEED_B = "ffeeddccbbaa99887766554433221100"


def run(command, expect_success=True):
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if expect_success and result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


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
        clang.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).stdout.strip()))
    opt = str(args.opt.resolve())
    fixture = Path(__file__).with_name("smoke.c").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    for level in ("O0", "O2"):
        outputs = []
        for label, seed in (("first", SEED_A), ("repeat", SEED_A),
                            ("different", SEED_B)):
            output = work_dir / f"{level}-{label}.ll"
            run([
                *clang, f"-{level}", "-mllvm", "-sub", "-mllvm", "-sobf",
                "-mllvm", f"-obf-test-seed={seed}", "-S", "-emit-llvm",
                str(fixture), "-o", str(output),
            ])
            run([opt, "-passes=verify", "-disable-output", str(output)])
            outputs.append(output.read_bytes())
        assert outputs[0] == outputs[1], f"{level}: same seed changed IR"
        assert outputs[0] != outputs[2], f"{level}: different seed did not change IR"
        print(f"[{level}] seeded obfuscation is reproducible and verifies", flush=True)

    for index, seed in enumerate((
        "00112233445566778899aabbccddeef",
        "xx00112233445566778899aabbccddeeff",
        "00112233445566778899aabbccddeefg",
    )):
        output = work_dir / f"invalid-{index}.ll"
        output.unlink(missing_ok=True)
        result = run([
            *clang, "-O0", "-mllvm", "-sub", "-mllvm",
            f"-obf-test-seed={seed}", "-S", "-emit-llvm", str(fixture),
            "-o", str(output),
        ], expect_success=False)
        assert result.returncode != 0, f"invalid test seed {index} was accepted"
        assert "obf-test-seed" in result.stderr, result.stderr
        assert not output.exists(), f"invalid test seed {index} emitted IR"
    print("invalid deterministic seeds are compiler errors", flush=True)


if __name__ == "__main__":
    main()
