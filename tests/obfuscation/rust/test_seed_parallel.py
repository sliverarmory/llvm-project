#!/usr/bin/env python3
"""Check fixed-seed final artifacts across parallel Rust codegen units."""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
from pathlib import Path


SEED = "00112233445566778899aabbccddeeff"
OTHER_SEED = "ffeeddccbbaa99887766554433221100"
SOURCE = Path(__file__).with_name("string_data.rs")
MARKERS = (b"m3-direct-secret", b"m3-static-", b"m3-const-",
           b"m3-bytes-", b"m3-ffi-")


def executable_path(work_dir, name):
    return work_dir / f"{name}{'.exe' if os.name == 'nt' else ''}"


def run(command, *, env=None):
    result = subprocess.run(command, env=env, capture_output=True, timeout=240)
    if result.returncode:
        raise AssertionError(
            f"failed {result.returncode}: {command!r}\n"
            f"stdout: {result.stdout[-1000:]!r}\n"
            f"stderr: {result.stderr[-3000:]!r}")
    return result


def branch_source(path):
    parts = []
    for number in range(16):
        parts.append(
            f"mod m{number} {{ #[unsafe(no_mangle)] #[inline(never)] "
            f"pub extern \"C\" fn probe_{number}(mut x:u64)->u64 {{ "
            f"for j in 0..31u64 {{ "
            f"x=x.rotate_left(((j+{number})%29+1) as u32)"
            f".wrapping_add(0x5a17+{number}); "
            f"if x&1==0 {{ x=x.wrapping_mul(7); }} "
            f"else {{ x=x.wrapping_mul(9); }} }} x }} }}")
    calls = "".join(
        f"x^=m{i}::probe_{i}(std::hint::black_box({i + 1}));"
        for i in range(16))
    parts.append(f"fn main(){{let mut x=0u64;{calls}println!(\"{{x}}\");}}")
    path.write_text("\n".join(parts) + "\n")


def instruction_hashes(objdump, binary):
    hashes = {}
    for number in range(16):
        raw = ("_" if platform.system() == "Darwin" else "") + f"probe_{number}"
        dump = run([str(objdump), f"--disassemble-symbols={raw}",
                    "--no-show-raw-insn", str(binary)]).stdout.decode()
        lines = re.findall(r"(?m)^\s*[0-9a-f]+:\s+(.+)$", dump)
        assert lines, (binary, raw)
        normalized = "\n".join(
            re.sub(r"0x[0-9a-f]+(?= <)", "ADDR", line) for line in lines)
        hashes[raw] = hashlib.sha256(normalized.encode()).hexdigest()
    return hashes


def compile_branches(rustc, objdump, source, work_dir, pass_name, seed,
                     suffix):
    binary = executable_path(work_dir, f"{pass_name}-{suffix}")
    events = work_dir / f"{pass_name}-{suffix}.jsonl"
    events.unlink(missing_ok=True)
    options = [f"-rust-obf-pipeline=obf-{pass_name}",
               "-rust-obf-prelink-only", f"-obf-test-seed={seed}",
               "-obf-only-functions=" + ",".join(f"probe_{i}" for i in range(16))]
    if pass_name == "bcf":
        options.extend(("-bcf_prob=100", "-bcf_max_blocks=48",
                        "-bcf_max_growth=2048"))
    exports = ([item for number in range(16)
                for item in ("-C", f"link-arg=/EXPORT:probe_{number}")]
               if os.name == "nt" else [])
    command = [str(rustc), "--edition=2024", "-C", "opt-level=2",
               "-C", "codegen-units=4", "-C", "lto=false",
               "-C", "llvm-args=" + " ".join(options), *exports,
               str(source), "-o", str(binary)]
    run(command, env={**os.environ, "RUST_OBF_EVENT_FILE": str(events)})
    records = [json.loads(line) for line in events.read_text().splitlines()]
    effects = {entry["raw_name"] for entry in records
               if entry["event"] == "effect" and entry["pass"] == pass_name}
    assert effects == {f"probe_{i}" for i in range(16)}, effects
    return run([str(binary)]).stdout, instruction_hashes(objdump, binary)


def check_branches(rustc, objdump, work_dir):
    source = work_dir / "parallel.rs"
    branch_source(source)
    baseline = executable_path(work_dir, "baseline")
    exports = ([item for number in range(16)
                for item in ("-C", f"link-arg=/EXPORT:probe_{number}")]
               if os.name == "nt" else [])
    baseline_command = [str(rustc), "--edition=2024", "-C", "opt-level=2",
                        "-C", "codegen-units=4", "-C", "lto=false",
                        *exports, str(source), "-o", str(baseline)]
    run(baseline_command)
    expected = run([str(baseline)]).stdout
    assert expected == b"3363439960029110486\n", expected

    seed_changed_code = False
    for pass_name in ("sub", "bcf", "fla"):
        first = None
        for number in range(3):
            output, witness = compile_branches(
                rustc, objdump, source, work_dir, pass_name, SEED,
                f"fixed-{number}")
            assert output == expected, (pass_name, output)
            if first is not None:
                assert witness == first, f"{pass_name}: fixed seed drifted"
            first = witness
        output, changed = compile_branches(
            rustc, objdump, source, work_dir, pass_name, OTHER_SEED,
            "other-seed")
        assert output == expected
        changed_code = any(changed[name] != first[name] for name in first)
        seed_changed_code |= changed_code
        print(f"PASS {pass_name}: 16/16 final symbols stable in three clean "
              f"parallel builds; other seed changes final code={changed_code}",
              flush=True)
    assert seed_changed_code, "different seed changed no final code witness"


def encoded_globals(ir, records):
    names = {entry["raw_name"] for entry in records
             if entry["event"] == "effect" and entry["pass"] == "sobf"
             and entry["kind"] == "global"}
    assert len(names) >= 5, names
    values = {}
    for name in names:
        match = re.search(r"(?m)^@" + re.escape(name) +
                          r' = .*? c"([^"]+)"', ir)
        assert match, name
        values[name] = match.group(1)
    decoder = re.findall(r"\.datadiv_decode\d+", ir)
    assert decoder, "string decoder missing"
    return values, sorted(set(decoder))


def compile_strings(rustc, work_dir, seed, suffix):
    binary = executable_path(work_dir, f"strings-{suffix}")
    llvm_ir = binary.with_suffix(".ll")
    events = work_dir / f"strings-{suffix}.jsonl"
    events.unlink(missing_ok=True)
    run([str(rustc), "--edition=2024", "-C", "opt-level=2",
         "-C", "codegen-units=4", "-C", "lto=false",
         "-C", "llvm-args=-rust-obf-pipeline=obf-string "
               f"-rust-obf-prelink-only -obf-test-seed={seed}",
         f"--emit=llvm-ir={llvm_ir},link", str(SOURCE), "-o", str(binary)],
        env={**os.environ, "RUST_OBF_EVENT_FILE": str(events)})
    assert all(marker not in binary.read_bytes() for marker in MARKERS)
    records = [json.loads(line) for line in events.read_text().splitlines()]
    return run([str(binary)]).stdout, encoded_globals(
        llvm_ir.read_text(), records)


def check_strings(rustc, work_dir):
    baseline = executable_path(work_dir, "strings-baseline")
    run([str(rustc), "--edition=2024", "-C", "opt-level=2",
         "-C", "codegen-units=4", "-C", "lto=false",
         str(SOURCE), "-o", str(baseline)])
    expected = run([str(baseline)]).stdout
    first = None
    for number in range(3):
        output, witness = compile_strings(rustc, work_dir, SEED,
                                          f"fixed-{number}")
        assert output == expected
        if first is not None:
            assert witness == first, "fixed seed changed encoded string data"
        first = witness
    output, changed = compile_strings(rustc, work_dir, OTHER_SEED,
                                      "other-seed")
    assert output == expected
    assert first[0] != changed[0], "different seed kept encoded string data"
    print(f"PASS string: {len(first[0])} encoded globals and decoder stable "
          "in three clean builds; plaintext absent; other seed changes data",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    rustc = args.rustc.resolve()
    version = run([str(rustc), "-vV"]).stdout.decode()
    assert re.search(r"(?m)^release: 1\.99\.", version), version
    assert re.search(r"(?m)^LLVM version: 23\.", version), version
    check_branches(rustc, args.objdump.resolve(), work_dir)
    check_strings(rustc, work_dir)


if __name__ == "__main__":
    main()
