"""Check string startup/storage safety and naked BCF exclusions."""

import argparse
import re
import subprocess
import sys
from pathlib import Path


SEED = "00112233445566778899aabbccddeeff"
SHORT_LITERAL = b"obf-short-literal"
LATE_LITERAL = b"late-constructor-data"
TARGETS = (
    "aarch64-apple-macosx13.0", "x86_64-pc-windows-msvc",
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
)


def run(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def function_body(ir, name):
    match = re.search(rf"(?ms)^define [^\n]*@{name}\([^)]*\)[^{{]*\{{(.*?)^}}", ir)
    assert match, f"Function {name} missing"
    return match.group(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    clang = [str(args.clang.resolve())]
    if sys.platform == "darwin":
        clang.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).stdout.strip()))
    opt = str(args.opt.resolve())
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    fixtures = Path(__file__).resolve().parent
    common = ["-fno-builtin", "-fno-discard-value-names"]
    obf_common = ["-mllvm", "-obf-report-skips", "-mllvm", f"-obf-test-seed={SEED}"]

    for level in ("O0", "O2"):
        for fixture in ("string_safety", "naked_safety"):
            results = {}
            for variant in ("baseline", "obfuscated"):
                stem = work_dir / f"{fixture}-{level}-{variant}"
                ir_path = stem.with_suffix(".ll")
                executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
                flags = []
                if variant == "obfuscated":
                    flags = [*obf_common]
                    if fixture == "string_safety":
                        flags.extend(("-mllvm", "-sobf"))
                    else:
                        flags.extend((
                            "-mllvm", "-bcf", "-mllvm", "-bcf_prob=100",
                            "-mllvm", "-obf-only-functions=naked_target,ordinary_target",
                        ))
                command = [*clang, f"-{level}", *common, *flags, str(fixtures / f"{fixture}.c")]
                emitted = run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
                run([opt, "-passes=verify", "-disable-output", str(ir_path)])
                run([*command, "-o", str(executable)])
                execution = run([str(executable)], timeout=10)
                results[variant] = (ir_path.read_text(encoding="utf-8"), executable.read_bytes())
                if fixture == "string_safety":
                    assert execution.stdout.strip() == SHORT_LITERAL.decode(), execution.stdout
                if variant != "obfuscated":
                    continue
                if fixture == "string_safety":
                    for symbol, reason in (("section_message", "explicit-section"),
                                           ("early_message", "early-initialization")):
                        assert f'symbol="{symbol}" reason={reason}' in emitted.stderr, emitted.stderr
                else:
                    assert 'symbol="naked_target" reason=naked' in emitted.stderr, emitted.stderr

            base_ir, base_binary = results["baseline"]
            obf_ir, obf_binary = results["obfuscated"]
            if fixture == "string_safety":
                for literal in (SHORT_LITERAL, LATE_LITERAL):
                    assert literal in base_binary, f"Baseline fixture lost {literal!r}"
                    assert literal not in obf_binary, f"{level} executable retains {literal!r}"
                    assert literal.decode() not in obf_ir, f"{level} IR retains {literal!r}"
                assert "load volatile i8" in obf_ir, "Decoder reads must survive optimization"
                assert "store volatile i8" in obf_ir, "Decoder writes must survive optimization"
                assert 'c"section-read-only\\00"' in obf_ir
                assert 'c"early-constructor-data\\00"' in obf_ir
            else:
                assert function_body(base_ir, "naked_target") == function_body(obf_ir, "naked_target")
                assert "load volatile i32" in function_body(obf_ir, "ordinary_target")
            print(f"{level} {fixture}: baseline and obfuscated runtime, IR, and final binary checks passed", flush=True)

    indirect_output = work_dir / "string-early-indirect.ll"
    result = run([
        opt, "-passes=obf-string,verify", "-obf-report-skips", "-S",
        str(fixtures / "string_early_indirect.ll"), "-o", str(indirect_output),
    ])
    assert 'symbol="message" reason=early-ctor-indirect-call' in result.stderr, result.stderr
    indirect_ir = indirect_output.read_text(encoding="utf-8")
    assert 'c"early-indirect-use\\00"' in indirect_ir
    assert ".datadiv_decode" not in indirect_ir
    print("Indirect priority-zero constructor: conservative skip verified", flush=True)

    # Frontend IR checks work without a code generator for the target. Native
    # CI builds may register only X86 or AArch64, so emit objects only when
    # the required backend is present.
    registered = run([str(args.clang.resolve()), "--print-targets"]).stdout
    backends = set(re.findall(r"(?m)^\s+([\w-]+)\s+-", registered))
    for target in TARGETS:
        backend = "aarch64" if target.startswith("aarch64") else "x86-64"
        can_emit_object = backend in backends
        for level in ("O0", "O2"):
            for fixture, flags in (
                ("string_safety", ("-mllvm", "-sobf")),
                ("naked_safety", (
                    "-mllvm", "-bcf", "-mllvm", "-bcf_prob=100",
                    "-mllvm", "-obf-only-functions=naked_target,ordinary_target",
                )),
            ):
                stem = work_dir / f"{fixture}-{target}-{level}"
                ir_path = Path(f"{stem}.ll")
                command = [
                    str(args.clang.resolve()), "-target", target, f"-{level}",
                    *common, *obf_common, *flags,
                    str(fixtures / f"{fixture}.c"),
                ]
                result = run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
                run([opt, "-passes=verify", "-disable-output", str(ir_path)])
                if can_emit_object:
                    run([*command, "-c", "-o", f"{stem}.o"])
                expected = ("explicit-section", "early-initialization") if fixture == "string_safety" else ("naked",)
                for reason in expected:
                    assert f"reason={reason}" in result.stderr, result.stderr
        object_status = ("object checks passed" if can_emit_object else
                         f"object checks skipped: {backend} backend is not registered")
        print(f"{target}: O0/O2 IR verified; {object_status}; cross-target runtime not run", flush=True)


if __name__ == "__main__":
    main()
