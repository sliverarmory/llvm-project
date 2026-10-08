"""Extract a Rust obfuscation archive, verify its manifest, and smoke-test it."""

import argparse
import json
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from verify_integrity import (sha256, validate_native_rustc_host,
                              validate_package_path, verify_archive_sidecar,
                              verify_tree)


ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((Path(__file__).parent / "pins.json").read_text())


def safe_member(name, *, is_dir=False):
    # Archive paths use forward slashes on every host. Reject alternate
    # Windows separators and drive names before either extractor normalizes.
    if is_dir:
        name = name.removesuffix("/")
    try:
        parts = validate_package_path(name)
    except ValueError as error:
        raise ValueError(f"unsafe archive entry: {name}") from error
    return parts[0]


def extract(archive, destination):
    roots = set()
    destination.mkdir(parents=True, exist_ok=False)
    if str(archive).endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            for member in members:
                roots.add(safe_member(member.name, is_dir=member.isdir()))
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f"archive contains unsupported entry: {member.name}")
            if len(roots) != 1:
                raise ValueError("archive must contain exactly one package root")
            source.extractall(destination, filter="data")
    elif archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                roots.add(safe_member(member.filename, is_dir=member.is_dir()))
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError(f"archive contains unsupported entry: {member.filename}")
            if len(roots) != 1:
                raise ValueError("archive must contain exactly one package root")
            source.extractall(destination)
    else:
        raise ValueError("archive must end in .tar.gz or .zip")
    return destination / roots.pop()


def verify_manifest(package, *, allow_dirty):
    manifest = verify_tree(package, allow_dirty=allow_dirty)
    if manifest["rust_source_commit"] != PINS["rust_commit"]:
        raise ValueError("archive Rust source does not match the pinned commit")
    if manifest["llvm_base_commit"] != PINS["llvm_base_commit"]:
        raise ValueError("archive LLVM base does not match the pinned commit")
    rust_version = manifest["rustc_vv"]
    if (f"release: {PINS['rust_version']}" not in rust_version or
            f"LLVM version: {PINS['llvm_version']}" not in rust_version):
        raise ValueError("archive compiler does not match the pinned versions")
    return manifest


def run(command, *, env=None, timeout=600):
    print("+", " ".join(map(str, command)), flush=True)
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command exited {result.returncode}: {command!r}\n"
                           f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    print(result.stdout[-2000:], end="" if result.stdout.endswith("\n") else "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--expected-commit",
                        help="require this exact LLVM source commit in the manifest")
    parser.add_argument("--full", action="store_true",
                        help="also run direct Rust, data, EH, and Cargo gates")
    parser.add_argument("--clang", type=Path,
                        help="built Clang tool for C/IR fixtures, required with --full")
    parser.add_argument("--live-https", action="store_true",
                        help="also fetch example.com with the extracted Rust client")
    parser.add_argument("--benchmark", action="store_true",
                        help="record extracted-toolchain cost comparison")
    parser.add_argument("--online", action="store_true",
                        help="allow pinned Cargo fixture registry downloads")
    args = parser.parse_args()
    if args.full and not args.clang:
        parser.error("--full requires --clang for the C and IR fixtures")
    if args.live_https and not args.full:
        parser.error("--live-https requires --full")
    archive = args.archive.resolve()
    work = args.work_dir.resolve()
    if work.exists() and any(work.iterdir()):
        raise ValueError(f"work directory is not empty: {work}")
    work.mkdir(parents=True, exist_ok=True)
    verify_archive_sidecar(archive)
    package = extract(archive, work / "extracted")
    manifest = verify_manifest(package, allow_dirty=args.allow_dirty)
    if (args.expected_commit and
            manifest["llvm_source_commit"] != args.expected_commit):
        raise ValueError("archive LLVM source commit differs from --expected-commit")
    exe = ".exe" if os.name == "nt" else ""
    rustc = package / "rust-toolchain" / "bin" / f"rustc{exe}"
    wrapper = package / "bin" / f"rust-obf-cargo{exe}"
    opt = package / "bin" / f"opt{exe}"
    objdump = package / "bin" / f"llvm-objdump{exe}"
    llvm_ar = package / "bin" / f"llvm-ar{exe}"
    llvm_dis = package / "bin" / f"llvm-dis{exe}"
    for tool in (rustc, wrapper, opt, objdump, llvm_ar, llvm_dis):
        if not tool.is_file():
            raise ValueError(f"packaged tool is absent: {tool}")
    rust_version = run([rustc, "-vV"]).stdout.strip()
    if rust_version != manifest["rustc_vv"]:
        raise ValueError("extracted rustc version differs from manifest")
    validate_native_rustc_host(rust_version)
    if run([opt, "--version"]).stdout.strip() != manifest["opt_version"]:
        raise ValueError("extracted opt version differs from manifest")
    if run([objdump, "--version"]).stdout.strip() != manifest["objdump_version"]:
        raise ValueError("extracted llvm-objdump version differs from manifest")
    run([sys.executable, ROOT / "tests/obfuscation/rust/run_pipeline.py",
         "--rustc", rustc, "--work-dir", work / "pipeline"])
    if args.full:
        run([sys.executable, ROOT / "tests/obfuscation/rust/test_pipeline.py",
             "--opt", opt, "--clang", args.clang.resolve()])
        run([sys.executable, ROOT / "tests/obfuscation/test_eh_regions.py",
             "--opt", opt, "--clang", args.clang.resolve(),
             "--work-dir", work / "eh-ir"])
        direct = [sys.executable, ROOT / "tests/obfuscation/rust/run_direct.py",
                  "--rustc", rustc, "--opt", opt, "--objdump", objdump,
                  "--work-dir", work / "direct"]
        if args.live_https:
            direct.append("--live-https")
        run(direct)
        run([sys.executable, ROOT / "tests/obfuscation/rust/test_string_data.py",
             "--opt", opt, "--clang", args.clang.resolve(), "--rustc", rustc,
             "--work-dir", work / "string-data"])
        run([sys.executable, ROOT / "tests/obfuscation/rust/run_eh_regions.py",
             "--rustc", rustc, "--opt", opt,
             "--work-dir", work / "eh-regions"])
        run([sys.executable, ROOT / "tests/obfuscation/rust/test_flatten_loop.py",
             "--rustc", rustc, "--opt", opt, "--objdump", objdump,
             "--work-dir", work / "flatten-loop"])
        cargo_command = [sys.executable, ROOT / "tests/obfuscation/rust/test_cargo.py",
                         "--wrapper", wrapper, "--rustc", rustc,
                         "--objdump", objdump, "--work-dir", work / "cargo"]
        if args.online:
            cargo_command.append("--online")
        run(cargo_command)
        run([sys.executable, ROOT / "tests/obfuscation/rust/test_lto.py",
             "--wrapper", wrapper, "--rustc", rustc, "--objdump", objdump,
             "--work-dir", work / "lto"], timeout=1800)
        run([sys.executable, ROOT / "tests/obfuscation/rust/test_seed_parallel.py",
             "--rustc", rustc, "--objdump", objdump,
             "--work-dir", work / "seed-parallel"], timeout=900)
        run([sys.executable,
             ROOT / "tests/obfuscation/rust/test_cargo_outputs.py",
             "--wrapper", wrapper, "--rustc", rustc, "--objdump", objdump,
             "--cc", args.clang.resolve(),
             "--work-dir", work / "cargo-outputs"], timeout=900)
    if args.benchmark:
        run([sys.executable, ROOT / "tests/obfuscation/rust/benchmark_rust.py",
             "--rustc", rustc, "--objdump", objdump,
             "--work-dir", work / "benchmark"])
    result = {"archive": str(archive), "archive_sha256": sha256(archive),
              "source_commit": manifest["llvm_source_commit"],
              "source_dirty": manifest["source_dirty"],
              "rustc_vv": rust_version, "full": args.full,
              "live_https": args.live_https, "benchmark": args.benchmark,
              "passed": True}
    (work / "smoke-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"PASS extracted toolchain smoke: {package}")


if __name__ == "__main__":
    main()
