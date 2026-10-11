"""Check string startup/storage safety and naked BCF exclusions."""

import argparse
import re
import subprocess
import sys
from pathlib import Path


SEED = "00112233445566778899aabbccddeeff"
SHORT_LITERAL = b"obf-short-literal"
LATE_LITERAL = b"late-constructor-data"
POOLED_LITERAL = b"pooled-inline-literal"
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


def check_string_safety(ir, diagnostics, label, binary=None):
    encoded = [SHORT_LITERAL, LATE_LITERAL]
    retained = [b"section-read-only", b"early-constructor-data"]
    if re.search(r'^target triple = "[^"]*windows', ir, re.MULTILINE):
        # Clang gives Windows inline literals linkonce_odr COMDAT storage.
        # Each translation unit could otherwise emit a decoder for the single
        # allocation retained by the linker, corrupting the shared bytes.
        pooled_line = next((line for line in ir.splitlines()
                            if line.startswith("@")
                            and f'c"{POOLED_LITERAL.decode()}\\00"' in line), None)
        assert pooled_line, f"{label}: missing pooled literal initializer"
        assert "linkonce_odr" in pooled_line and "comdat" in pooled_line, pooled_line
        assert " constant " in pooled_line, pooled_line
        symbol = pooled_line.split(" = ", 1)[0][1:].strip('"')
        assert f'symbol="{symbol}" reason=weak-or-comdat' in diagnostics, diagnostics
        retained.append(POOLED_LITERAL)
    else:
        encoded.append(POOLED_LITERAL)

    for literal in encoded:
        assert literal.decode() not in ir, f"{label}: IR retains {literal!r}"
        if binary is not None:
            assert literal not in binary, f"{label}: binary retains {literal!r}"
    for literal in retained:
        assert f'c"{literal.decode()}\\00"' in ir, f"{label}: IR lost skipped {literal!r}"
        if binary is not None:
            assert literal in binary, f"{label}: binary lost skipped {literal!r}"
    assert "load volatile i8" in ir, f"{label}: decoder reads must survive optimization"
    assert "store volatile i8" in ir, f"{label}: decoder writes must survive optimization"
    for symbol, reason in (("section_message", "explicit-section"),
                           ("early_message", "early-initialization")):
        assert f'symbol="{symbol}" reason={reason}' in diagnostics, diagnostics


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
                results[variant] = (ir_path.read_text(encoding="utf-8"), executable.read_bytes(), emitted.stderr)
                if fixture == "string_safety":
                    assert execution.stdout.splitlines() == [
                        SHORT_LITERAL.decode(), POOLED_LITERAL.decode(),
                    ], execution.stdout
                if variant != "obfuscated":
                    continue
                if fixture != "string_safety":
                    assert 'symbol="naked_target" reason=naked' in emitted.stderr, emitted.stderr

            base_ir, base_binary, _ = results["baseline"]
            obf_ir, obf_binary, diagnostics = results["obfuscated"]
            if fixture == "string_safety":
                for literal in (SHORT_LITERAL, LATE_LITERAL, POOLED_LITERAL):
                    assert literal in base_binary, f"Baseline fixture lost {literal!r}"
                check_string_safety(obf_ir, diagnostics, f"{level} executable", obf_binary)
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
                object_bytes = None
                if can_emit_object:
                    object_path = Path(f"{stem}.o")
                    run([*command, "-c", "-o", str(object_path)])
                    object_bytes = object_path.read_bytes()
                if fixture == "string_safety":
                    check_string_safety(ir_path.read_text(encoding="utf-8"), result.stderr,
                                        f"{target} {level} object", object_bytes)
                else:
                    assert 'symbol="naked_target" reason=naked' in result.stderr, result.stderr
        object_status = ("object checks passed" if can_emit_object else
                         f"object checks skipped: {backend} backend is not registered")
        print(f"{target}: O0/O2 IR verified; {object_status}; cross-target runtime not run", flush=True)


if __name__ == "__main__":
    main()
