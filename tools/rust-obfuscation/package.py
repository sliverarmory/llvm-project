"""Build a provenance-bearing, relocatable Rust obfuscation toolchain archive."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from verify_integrity import validate_native_rustc_host, validate_package_path


ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((Path(__file__).parent / "pins.json").read_text())


def run(*args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                            check=True, timeout=30)
    return result.stdout.strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ignored_sources(directory, names):
    if Path(directory).name == "rustlib":
        return [name for name in ("src", "rustc-src") if name in names]
    return []


def copy_tool(source, destination):
    if not source.is_file():
        raise ValueError(f"required tool is absent: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def manifest_files(package_dir):
    files = []
    for path in sorted(package_dir.rglob("*")):
        validate_package_path(path.relative_to(package_dir).as_posix())
        if path.is_symlink():
            raise ValueError(f"archive would contain an external symlink: {path}")
        if path.is_file() and path != package_dir / "manifest.json":
            files.append({"path": path.relative_to(package_dir).as_posix(),
                          "bytes": path.stat().st_size,
                          "sha256": sha256(path)})
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage1", type=Path, required=True)
    parser.add_argument("--rust-source", type=Path, default=ROOT / "build-rust-1.99",
                        help="pinned Rust 1.99 source checkout for license and provenance")
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="archive path ending in .tar.gz or .zip")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="mark a local development archive as dirty")
    args = parser.parse_args()

    output = args.output.resolve()
    if not (str(output).endswith(".tar.gz") or output.suffix == ".zip"):
        parser.error("--output must end in .tar.gz or .zip")
    output.parent.mkdir(parents=True, exist_ok=True)

    stage1 = args.stage1.resolve()
    rust_source = args.rust_source.resolve()
    wrapper = args.wrapper.resolve()
    opt = args.opt.resolve()
    objdump = args.objdump.resolve()
    exe = ".exe" if os.name == "nt" else ""
    llvm_ar = objdump.with_name("llvm-ar" + exe)
    llvm_dis = objdump.with_name("llvm-dis" + exe)
    for tool in (stage1 / "bin" / f"rustc{exe}", wrapper, opt, objdump,
                 llvm_ar, llvm_dis):
        if not tool.is_file():
            raise ValueError(f"required tool is absent: {tool}")
    source_revision = run("git", "-C", str(rust_source), "rev-parse", "HEAD")
    if source_revision != PINS["rust_commit"]:
        raise ValueError(f"Rust source is {source_revision}; expected {PINS['rust_commit']}")
    if run("git", "-C", str(rust_source), "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("pinned Rust source has tracked modifications")

    revision = run("git", "rev-parse", "HEAD")
    dirty = bool(run("git", "status", "--porcelain"))
    if dirty and not args.allow_dirty:
        raise ValueError("source tree is dirty; commit the milestone or pass --allow-dirty for a local test")
    rust_version = run(str(stage1 / "bin" / f"rustc{exe}"), "-vV")
    if (f"release: {PINS['rust_version']}" not in rust_version or
            f"LLVM version: {PINS['llvm_version']}" not in rust_version):
        raise ValueError("stage1 does not match the pinned Rust/LLVM versions")
    validate_native_rustc_host(rust_version)

    label = f"rust-obfuscation-{revision[:12]}-{platform.system().lower()}-{platform.machine().lower()}"
    with tempfile.TemporaryDirectory(prefix="rust-obf-package-", dir=output.parent) as temp:
        package_dir = Path(temp) / label
        shutil.copytree(stage1, package_dir / "rust-toolchain",
                        ignore=ignored_sources)
        for source, name in ((wrapper, f"rust-obf-cargo{exe}"),
                             (opt, f"opt{exe}"),
                             (objdump, f"llvm-objdump{exe}"),
                             (llvm_ar, f"llvm-ar{exe}"),
                             (llvm_dis, f"llvm-dis{exe}")):
            copy_tool(source, package_dir / "bin" / name)
        copy_tool(ROOT / "tools/rust-obfuscation/cargo-wrapper/README.md",
                  package_dir / "CARGO-WRAPPER.md")
        copy_tool(ROOT / "tools/rust-obfuscation/README.md",
                  package_dir / "BUILD.md")
        copy_tool(ROOT / "tools/rust-obfuscation/DELIVERY.md",
                  package_dir / "DELIVERY.md")
        copy_tool(ROOT / "tools/rust-obfuscation/pins.json",
                  package_dir / "pins.json")
        copy_tool(ROOT / "tests/obfuscation/rust/PIPELINE.md",
                  package_dir / "PIPELINE.md")
        copy_tool(ROOT / "tests/obfuscation/rust/STRING_DATA.md",
                  package_dir / "STRING_DATA.md")
        copy_tool(ROOT / "tests/obfuscation/rust/EH_REGIONS.md",
                  package_dir / "EH_REGIONS.md")
        copy_tool(ROOT / "tests/obfuscation/rust/CARGO_OUTPUTS.md",
                  package_dir / "CARGO_OUTPUTS.md")
        copy_tool(ROOT / "tools/rust-obfuscation/verify_integrity.py",
                  package_dir / "verify_integrity.py")
        for name in ("COPYRIGHT", "LICENSE-APACHE", "LICENSE-MIT"):
            copy_tool(rust_source / name, package_dir / "licenses" / "rust" / name)
        copy_tool(ROOT / "llvm/LICENSE.TXT", package_dir / "licenses" / "llvm" / "LICENSE.TXT")
        provenance = {
            "schema_version": 1,
            "llvm_source_commit": revision,
            "source_dirty": dirty,
            "rust_source_commit": source_revision,
            "llvm_base_commit": PINS["llvm_base_commit"],
            "rustc_vv": rust_version,
            "opt_version": run(str(opt), "--version"),
            "objdump_version": run(str(objdump), "--version"),
            "host_system": platform.system(),
            "host_machine": platform.machine(),
            "files": manifest_files(package_dir),
        }
        (package_dir / "manifest.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n")

        if str(output).endswith(".tar.gz"):
            with tarfile.open(output, "w:gz") as archive:
                archive.add(package_dir, arcname=label)
        else:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as archive:
                for path in sorted(package_dir.rglob("*")):
                    if path.is_file():
                        archive.write(path, arcname=f"{label}/{path.relative_to(package_dir).as_posix()}")

    digest = sha256(output)
    (output.parent / (output.name + ".sha256")).write_text(
        f"{digest}  {output.name}\n")
    print(f"archive={output}")
    print(f"sha256={digest}")
    print(f"source_commit={revision}")
    print(f"source_dirty={dirty}")


if __name__ == "__main__":
    main()
