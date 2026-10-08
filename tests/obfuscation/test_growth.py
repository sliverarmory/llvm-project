"""Check per-function IR growth limits and dead substitution cleanup."""

import argparse
import re
import subprocess
import sys
from pathlib import Path


SEED = "00112233445566778899aabbccddeeff"


def run(command, timeout=45):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def body(ir, name):
    match = re.search(
        rf"(?ms)^define [^\n]*@{re.escape(name)}\([^\n]*\) [^\n]*\{{\n(.*?)^\}}",
        ir,
    )
    assert match, f"missing {name} in emitted IR"
    return match.group(1)


def instruction_count(function_body):
    return sum(line.startswith("  ") and not line.startswith("  ;")
               for line in function_body.splitlines())


def block_count(function_body):
    return len(re.findall(r"(?m)^[^ ;\n][^:\n]*:", function_body))


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
    source = Path(__file__).with_name("growth.ll").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    for level in ("O0", "O2"):
        emitted = {}
        options_by_name = {
            "baseline": (),
            "sub-capped": ("-sub", "-sub_loop=100000", "-sub_max_growth=64"),
            "sub-zero": ("-sub", "-sub_loop=100000", "-sub_max_growth=0"),
            "bcf-capped": ("-bcf", "-bcf_prob=100", "-bcf_loop=100000",
                           "-bcf_max_blocks=3", "-bcf_max_growth=256"),
            "bcf-zero": ("-bcf", "-bcf_prob=100", "-bcf_loop=100000",
                         "-bcf_max_blocks=0", "-bcf_max_growth=0"),
        }
        for name, options in options_by_name.items():
            flags = [item for option in options for item in ("-mllvm", option)]
            flags.extend(("-mllvm", f"-obf-test-seed={SEED}"))
            output = work_dir / f"{level}-{name}.ll"
            command = [*clang, "-x", "ir", f"-{level}", *flags, str(source)]
            run([*command, "-S", "-emit-llvm", "-o", str(output)])
            run([opt, "-passes=verify", "-disable-output", str(output)])
            emitted[name] = output.read_text(encoding="utf-8")
            if name in ("sub-capped", "bcf-capped"):
                executable = work_dir / f"{level}-{name}"
                if sys.platform == "win32":
                    executable = executable.with_suffix(".exe")
                run([*command, "-o", str(executable)])
                run([str(executable)], timeout=10)

        base_sub = body(emitted["baseline"], "sub_target")
        capped_sub = body(emitted["sub-capped"], "sub_target")
        assert instruction_count(capped_sub) <= instruction_count(base_sub) + 64
        for original in ("original_add", "original_sub", "original_mul",
                         "original_and"):
            assert f"%{original} =" not in capped_sub, (
                f"{level}: dead original %{original} remains after substitution"
            )
        assert body(emitted["sub-zero"], "sub_target") == base_sub

        base_bcf = body(emitted["baseline"], "bcf_target")
        capped_bcf = body(emitted["bcf-capped"], "bcf_target")
        assert block_count(capped_bcf) <= block_count(base_bcf) + 3
        assert instruction_count(capped_bcf) <= instruction_count(base_bcf) + 256
        assert "alteredBB" in capped_bcf and "urem i32" in capped_bcf, (
            f"{level}: bounded BCF did not finalize its opaque predicate"
        )
        assert body(emitted["bcf-zero"], "bcf_target") == base_bcf
        print(f"[{level}] substitution and BCF growth stays bounded", flush=True)


if __name__ == "__main__":
    main()
