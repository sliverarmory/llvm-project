"""Check that string obfuscation preserves Objective-C runtime metadata."""

import argparse
import subprocess
from pathlib import Path


RUNTIMES = {
    "apple": ("x86_64-apple-macosx13.0", "macosx-10.13.0"),
    "gnustep": ("x86_64-unknown-linux-gnu", "gnustep-2.0"),
    "gcc": ("x86_64-unknown-linux-gnu", "gcc"),
}


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    source = Path(__file__).with_name("objc_metadata.m").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    for runtime, (target, objc_runtime) in RUNTIMES.items():
        for language in ("objective-c", "objective-c++"):
            for level in ("O0", "O2"):
                ir_path = work_dir / f"{runtime}-{language}-{level}.ll"
                run(
                    [
                        str(args.clang.resolve()),
                        "-target", target,
                        f"-fobjc-runtime={objc_runtime}",
                        "-x", language,
                        f"-{level}",
                        "-mllvm", "-sobf",
                        "-S", "-emit-llvm", str(source),
                        "-o", str(ir_path),
                    ]
                )
                run(
                    [str(args.opt.resolve()), "-passes=verify", "-disable-output", str(ir_path)]
                )
                ir = ir_path.read_text(encoding="utf-8")
                assert 'c"ObfuscationProbe\\00"' in ir, ir_path
                assert 'c"statusForURL:\\00"' in ir, ir_path
                if runtime == "apple":
                    assert (
                        'c"obf-runtime-literal\\00", section "__TEXT,__cstring' in ir
                    ), ir_path
                    assert "@_unnamed_cfstring_" in ir, ir_path
                if runtime == "gcc":
                    assert 'c"AnotherHack\\00"' in ir, ir_path
                    assert 'c"__ObjC_Protocol_Holder_Ugly_Hack\\00"' in ir, ir_path
                assert "obf-user-string-marker" not in ir, ir_path
                assert ".datadiv_decode" in ir, ir_path
                print(f"{runtime} {language} {level}: ObjC metadata preserved", flush=True)


if __name__ == "__main__":
    main()
