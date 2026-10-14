"""Invalid obfuscation numeric options must fail at LLVM command-line parsing."""

import argparse
import subprocess
from pathlib import Path


CASES = (
    ("sub_loop", "sub", ("0", "-1"), ("1",)),
    ("split_num", "split", ("1", "11", "-1"), ("2", "10")),
    ("bcf_prob", "bcf", ("0", "101", "-1"), ("1", "100")),
    ("bcf_loop", "bcf", ("0", "-1"), ("1",)),
)


def compile_ir(clang, source, output, level, options):
    flags = [item for option in options for item in ("-mllvm", option)]
    return subprocess.run(
        [clang, "-x", "ir", f"-{level}", *flags, "-S", "-emit-llvm",
         str(source), "-o", str(output)],
        capture_output=True, text=True, timeout=120,
    )


def check_bcf_probability(clang, work_dir):
    source = work_dir / "bcf-probability.ll"
    source.write_text(
        "define i32 @target(i32 %x) {\n"
        "entry:\n"
        "  %a = add i32 %x, 1\n"
        "  ret i32 %a\n"
        "}\n", encoding="utf-8",
    )
    # The bcf/target test stream draws 99, so a 99% rate skips this block.
    seed = "00000000000000000000000000000024"
    for probability, transformed in ((99, False), (100, True)):
        output = work_dir / f"bcf-probability-{probability}.ll"
        result = compile_ir(
            clang, source, output, "O0",
            ("-bcf", f"-bcf_prob={probability}", f"-obf-test-seed={seed}"),
        )
        assert result.returncode == 0, result.stderr
        ir = output.read_text(encoding="utf-8")
        assert ("originalBBalteredBB" in ir) == transformed, (
            f"-bcf_prob={probability} selected the wrong block set"
        )
        assert ("@.obf.bcf.x = private global i32 0" in ir) == transformed
        assert ("@.obf.bcf.y = private global i32 0" in ir) == transformed
    print("BCF 99% can skip while 100% always selects", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    clang = str(args.clang.resolve())
    source = Path(__file__).with_name("substitution_undef.ll").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    for level in ("O0", "O2"):
        for option, enable, invalid, valid in CASES:
            for value in valid:
                output = work_dir / f"{level}-{option}-{value}-valid.ll"
                result = compile_ir(clang, source, output, level,
                                    (f"-{option}={value}",))
                assert result.returncode == 0 and output.exists(), (
                    f"{level}: valid -{option}={value} failed: {result.stderr}"
                )
            for value in invalid:
                for enabled in (False, True):
                    suffix = "enabled" if enabled else "standalone"
                    output = work_dir / f"{level}-{option}-{value}-{suffix}.ll"
                    output.unlink(missing_ok=True)
                    options = (f"-{option}={value}",)
                    if enabled:
                        options = (f"-{enable}", *options)
                    result = compile_ir(clang, source, output, level, options)
                    assert result.returncode != 0, (
                        f"{level}: invalid -{option}={value} compiled with {suffix}"
                    )
                    assert f"-{option}" in result.stderr, result.stderr
                    assert not output.exists(), (
                        f"{level}: invalid -{option}={value} emitted IR"
                    )
            print(f"[{level}] {option} rejects invalid values", flush=True)
    check_bcf_probability(clang, work_dir)


if __name__ == "__main__":
    main()
