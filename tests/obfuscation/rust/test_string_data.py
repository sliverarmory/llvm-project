#!/usr/bin/env python3
"""Verify Rust-style byte-array eligibility, exact bytes, and final artifacts.

The opt/Clang checks run in the portable suite. Pass --rustc to also check the
Rust 1.99 PassBuilder hook and a C consumer of an obfuscated Rust staticlib.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SEED = "00112233445566778899aabbccddeeff"
IR_MARKERS = (
    b"rust-ascii-secret-29db",
    "rust-utf8-β-漢字".encode(),
    b"rust-nul-secret\0tail",
    b"rust-byte-secret-\xff\0\x80",
)
RUST_CHUNKS = (
    "m3-direct-secret-β\0tail".encode(),
    "m3-static-ø-δ".encode(),
    "m3-const-漢字".encode(),
    b"m3-bytes-\0\xff\x80",
    b"m3-ffi-\0\xfe",
)
SKIPS = {
    "exported": "non-local",
    "weak": "weak-or-comdat",
    "early": "early-initialization",
    "metadata_data": "unsafe-global-user",
    "orphan": "no-runtime-use",
    "mixed": "function-not-selected",
    "descriptor_data": "unsafe-global-user",
    "sectioned": "explicit-section",
    "ptrint_data": "unsupported-constant-use",
}


def run(command, *, env=None, timeout=180):
    result = subprocess.run(command, capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"failed ({result.returncode}): {command!r}\n"
            f"stdout: {result.stdout.decode(errors='replace')}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
    return result


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def clang_prefix(clang, sysroot):
    prefix = [str(clang)]
    if sysroot:
        prefix.extend(("-isysroot", str(sysroot)))
    elif sys.platform == "darwin":
        prefix.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).stdout.decode().strip()))
    return prefix


def check_opt(opt, clang, work):
    fixture = ROOT / "string_data.ll"
    legacy = work / "legacy.ll"
    run([str(opt), f"-obf-test-seed={SEED}", "-passes=obf-string,verify",
         "-S", str(fixture), "-o", str(legacy)])
    legacy_ir = legacy.read_bytes()
    for marker in (b"rust-ascii-secret-29db", b"rust-utf8-", b"rust-nul-secret"):
        require(marker in legacy_ir, f"legacy C-string rule changed for {marker!r}")
    require(b".datadiv_decode" not in legacy_ir,
            "legacy C-string path encoded a non-NUL byte array")

    transformed = work / "rust-bytes.ll"
    events = work / "rust-byte-events.jsonl"
    events.unlink(missing_ok=True)
    env = os.environ.copy()
    env["RUST_OBF_EVENT_FILE"] = str(events)
    run([str(opt), f"-obf-test-seed={SEED}", "-sobf-rust-bytes",
         "-passes=obf-string,verify", "-S", str(fixture),
         "-o", str(transformed)], env=env)
    ir = transformed.read_bytes()
    for marker in (b"rust-ascii-secret-29db", b"rust-utf8-", b"rust-nul-secret",
                   b"rust-byte-secret-"):
        require(marker not in ir, f"selected Rust byte array remained in IR: {marker!r}")
    require(b"@static_ref = private constant <{ ptr, [8 x i8] }> "
            b"<{ ptr @ascii, [8 x i8] c\"\\16\\00\\00\\00\\00\\00\\00\\00\" }>" in ir,
            "static slice pointer/length changed")
    require(b"@const_ref = private constant <{ ptr, [8 x i8] }> "
            b"<{ ptr @utf8, [8 x i8] c\"\\13\\00\\00\\00\\00\\00\\00\\00\" }>" in ir,
            "const slice pointer/length changed")
    require(b".datadiv_decode" in ir and b"@llvm.global_ctors" in ir,
            "byte decoder was not registered")
    effects = [json.loads(line) for line in events.read_text().splitlines()
               if line.strip()]
    encoded = {e["raw_name"] for e in effects
               if e.get("event") == "effect" and e.get("pass") == "sobf"}
    require(encoded == {"ascii", "utf8", "nul", "bytes"},
            f"unexpected byte-array effects: {encoded!r}")

    repeated = work / "rust-bytes-twice.ll"
    repeat_events = work / "rust-byte-repeat-events.jsonl"
    repeat_events.unlink(missing_ok=True)
    repeat_env = os.environ.copy()
    repeat_env["RUST_OBF_EVENT_FILE"] = str(repeat_events)
    run([str(opt), f"-obf-test-seed={SEED}", "-sobf-rust-bytes",
         "-passes=obf-string,obf-string,verify", "-S", str(fixture),
         "-o", str(repeated)], env=repeat_env)
    repeat_ir = repeated.read_text()
    repeat_records = [json.loads(line) for line in repeat_events.read_text().splitlines()
                      if line.strip()]
    repeat_effects = [record for record in repeat_records
                      if record.get("event") == "effect"]
    require(repeat_ir.count("define private void @.datadiv_decode") == 1 and
            len(repeat_effects) == 4,
            "repeated Rust string pass generated another decoder or effect")

    for level in ("-O0", "-O2"):
        for label, path in (("baseline", fixture), ("encoded", transformed)):
            executable = work / f"ir-{label}-{level[1:]}"
            if sys.platform == "win32":
                executable = executable.with_suffix(".exe")
            run([*clang, "-x", "ir", level, str(path), "-o", str(executable)])
            run([str(executable)], timeout=15)
            data = executable.read_bytes()
            for marker in IR_MARKERS:
                if label == "baseline":
                    require(marker in data,
                            f"baseline lacks final-artifact witness {marker!r}")
                else:
                    require(marker not in data,
                            f"encoded artifact retained plaintext {marker!r}")
    print("PASS opt Rust byte arrays: exact runtime bytes, descriptors, effects, artifacts", flush=True)

    skip_fixture = ROOT / "string_data_skips.ll"
    skipped = work / "rust-skips.ll"
    names = ",".join(SKIPS)
    result = run([
        str(opt), f"-obf-test-seed={SEED}", "-sobf-rust-bytes",
        f"-sobf-only-globals={names}", "-obf-only-functions=selected",
        "-obf-report-skips", "-passes=obf-string,verify", "-S",
        str(skip_fixture), "-o", str(skipped),
    ])
    diagnostics = result.stderr.decode()
    skipped_ir = skipped.read_text()
    for name, reason in SKIPS.items():
        require(f'symbol="{name}" reason={reason}' in diagnostics,
                f"{name}: expected skip reason {reason}, got:\n{diagnostics}")
        require(re.search(rf"(?m)^@{name} = [^\n]*\bconstant\b", skipped_ir),
                f"{name}: exclusion changed constant storage")
    require(".datadiv_decode" not in skipped_ir,
            "excluded array unexpectedly gained a decoder")

    indirect = work / "rust-indirect-ctor.ll"
    result = run([
        str(opt), f"-obf-test-seed={SEED}", "-sobf-rust-bytes",
        "-sobf-only-globals=indirect_data", "-obf-report-skips",
        "-passes=obf-string,verify", "-S",
        str(ROOT / "string_data_indirect_ctor.ll"), "-o", str(indirect),
    ])
    require('symbol="indirect_data" reason=early-ctor-indirect-call'
            in result.stderr.decode(),
            "priority-zero indirect constructor was not excluded")
    print("PASS opt Rust byte arrays: bounded selection and documented exclusions", flush=True)


def parse_chunks(payload):
    chunks = []
    offset = 0
    while offset < len(payload):
        require(offset + 4 <= len(payload), "truncated Rust output length")
        size = int.from_bytes(payload[offset:offset + 4], "little")
        offset += 4
        require(offset + size <= len(payload), "truncated Rust output data")
        chunks.append(payload[offset:offset + size])
        offset += size
    return tuple(chunks)


def compile_rust(rustc, source, stem, *, level, llvm_options=(), crate_type=None):
    ir_path = stem.with_suffix(".ll")
    artifact = stem.with_suffix(".a") if crate_type else stem
    if not crate_type and sys.platform == "win32":
        artifact = artifact.with_suffix(".exe")
    command = [str(rustc), "--edition=2021", "--crate-name", stem.name.replace("-", "_"),
               "-C", f"opt-level={level}", "-C", "codegen-units=1",
               "-C", "panic=abort", "-C", "debuginfo=0"]
    if crate_type:
        command.extend(("--crate-type", crate_type))
    if llvm_options:
        command.extend(("-C", f"llvm-args={' '.join(llvm_options)}"))
    command.extend((f"--emit=llvm-ir={ir_path},link={artifact}", str(source)))
    run(command, timeout=360)
    return ir_path, artifact


def check_rust(rustc, opt, clang, work):
    version = run([str(rustc), "-vV"]).stdout.decode()
    require(re.search(r"(?m)^release: 1\.99\.", version) and
            re.search(r"(?m)^LLVM version: 23\.", version),
            f"expected the pinned Rust 1.99/LLVM 23 toolchain, got:\n{version}")
    source = ROOT / "string_data.rs"
    for level in (0, 2):
        baseline_ir, baseline = compile_rust(
            rustc, source, work / f"rust-O{level}-baseline", level=level)
        encoded_ir, encoded = compile_rust(
            rustc, source, work / f"rust-O{level}-encoded", level=level,
            llvm_options=("-rust-obf-pipeline=obf-string", f"-obf-test-seed={SEED}"))
        for path in (baseline_ir, encoded_ir):
            run([str(opt), "-passes=verify", "-disable-output", str(path)])
        for label, artifact in (("baseline", baseline), ("encoded", encoded)):
            chunks = parse_chunks(run([str(artifact)], timeout=15).stdout)
            require(chunks == RUST_CHUNKS,
                    f"Rust O{level}/{label} output bytes changed: {chunks!r}")
            binary = artifact.read_bytes()
            for marker in RUST_CHUNKS:
                require((marker in binary) == (label == "baseline"),
                        f"Rust O{level}/{label} final artifact witness failed: {marker!r}")
        print(f"PASS Rust O{level}: exact bytes and absent plaintext in final executable", flush=True)

    ffi_source = ROOT / "string_data_ffi.rs"
    ffi_marker = b"m3-ffi-library-\0\xfe\x80"
    for label, options in (("baseline", ()),
                           ("encoded", ("-rust-obf-pipeline=obf-string",
                                        f"-obf-test-seed={SEED}"))):
        ir_path, library = compile_rust(
            rustc, ffi_source, work / f"ffi-{label}", level=2,
            llvm_options=options, crate_type="staticlib")
        run([str(opt), "-passes=verify", "-disable-output", str(ir_path)])
        require((b"m3-ffi-library-" in ir_path.read_bytes()) ==
                (label == "baseline"),
                f"Rust FFI {label} IR plaintext witness failed")
        require((ffi_marker in library.read_bytes()) == (label == "baseline"),
                f"Rust FFI {label} archive witness failed")
        executable = work / f"ffi-consumer-{label}"
        if sys.platform == "win32":
            executable = executable.with_suffix(".exe")
        run([*clang, str(ROOT / "string_data_ffi.c"), str(library),
             "-o", str(executable)], timeout=360)
        run([str(executable)], timeout=15)
    print("PASS Rust staticlib: C consumer sees exact byte length and contents", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--rustc", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    clang = clang_prefix(args.clang.resolve(), args.sysroot)
    check_opt(args.opt.resolve(), clang, work)
    if args.rustc:
        check_rust(args.rustc.absolute(), args.opt.resolve(), clang, work)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
