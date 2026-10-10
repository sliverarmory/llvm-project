#!/usr/bin/env python3
"""Verify a delivered Rust obfuscation tree and optional archive sidecar.

This script ships inside the archive and needs only Python's standard library.
It checks integrity against the adjacent manifest and checksum sidecar; these
hashes do not authenticate who produced the archive.
"""

import argparse
import hashlib
import json
import platform
import re
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
WINDOWS_DEVICES.update(f"{kind}{number}" for kind in ("COM", "LPT")
                       for number in range(1, 10))
WINDOWS_DEVICES.update(f"{kind}{number}" for kind in ("COM", "LPT")
                       for number in ("¹", "²", "³"))


def validate_package_path(name):
    """Reject archive names that escape or alias paths on supported hosts."""
    parts = name.split("/")
    if (not name or name.startswith("/") or "\\" in name or
            any(part in ("", ".", "..") for part in parts)):
        raise ValueError(f"unsafe package path: {name}")
    for part in parts:
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if (part.endswith((" ", ".")) or stem in WINDOWS_DEVICES or
                any(ord(char) < 32 or char in '<>:"|?*' for char in part)):
            raise ValueError(f"unsafe package path: {name}")
    return parts


def validate_native_rustc_host(version, *, system=None, machine=None):
    """Require rustc's host triple to match the native delivery platform."""
    system = platform.system() if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    hosts = {
        ("Darwin", "arm64"): "aarch64-apple-darwin",
        ("Darwin", "aarch64"): "aarch64-apple-darwin",
        ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("Linux", "amd64"): "x86_64-unknown-linux-gnu",
        ("Linux", "arm64"): "aarch64-unknown-linux-gnu",
        ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("Windows", "x86_64"): "x86_64-pc-windows-msvc",
        ("Windows", "amd64"): "x86_64-pc-windows-msvc",
    }
    expected = hosts.get((system, machine))
    if expected is None:
        raise ValueError(f"unsupported native delivery host: {system}/{machine}")
    match = re.search(r"(?m)^host: ([^\r\n]+)$", version)
    actual = match.group(1) if match else None
    if actual != expected:
        raise ValueError(f"rustc host {actual!r} does not match native {expected}")
    return expected


def verify_tree(package, *, expected_commit=None, allow_dirty=False):
    manifest = json.loads((package / "manifest.json").read_text())
    if manifest["schema_version"] != 1:
        raise ValueError("unsupported manifest schema")
    if manifest["source_dirty"] and not allow_dirty:
        raise ValueError("package was built from a dirty source checkout")
    if expected_commit and manifest["llvm_source_commit"] != expected_commit:
        raise ValueError("LLVM source commit differs from --expected-commit")

    expected = {}
    for entry in manifest["files"]:
        name = entry["path"]
        try:
            validate_package_path(name)
        except ValueError as error:
            raise ValueError(f"unsafe or duplicate manifest path: {name}") from error
        if name in expected:
            raise ValueError(f"unsafe or duplicate manifest path: {name}")
        expected[name] = entry
    actual = {}
    for path in package.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"package contains a symlink: {path}")
        if path.is_file() and path != package / "manifest.json":
            actual[path.relative_to(package).as_posix()] = path
    if set(actual) != set(expected):
        raise ValueError(
            f"package file list differs: missing={set(expected) - set(actual)}, "
            f"extra={set(actual) - set(expected)}"
        )
    for name, path in actual.items():
        entry = expected[name]
        if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"package file checksum mismatch: {name}")
    provenance = manifest.get("build_provenance")
    if provenance:
        stage_path = package / "rust-toolchain" / ".obfuscation-build.json"
        wrapper_records = list((package / "bin").glob("rust-obf-cargo*.provenance.json"))
        if len(wrapper_records) != 1:
            raise ValueError("package must contain exactly one wrapper build record")
        wrapper_path = wrapper_records[0]
        if (sha256(stage_path) != provenance["stage1_record_sha256"] or
                sha256(wrapper_path) != provenance["wrapper_record_sha256"]):
            raise ValueError("package build record hash differs from manifest")
        stage = json.loads(stage_path.read_text())
        wrapper = json.loads(wrapper_path.read_text())
        for key, value in (("source_identity", stage["source_identity"]),
                           ("stage1_build_revision", stage["build_revision"]),
                           ("wrapper_source_identity", wrapper["source_identity"]),
                           ("wrapper_build_revision", wrapper["build_revision"])):
            if provenance[key] != value:
                raise ValueError(f"package build record differs from manifest: {key}")
        delivered_stage = {name.removeprefix("rust-toolchain/"): {"bytes": entry["bytes"], "sha256": entry["sha256"]}
                           for name, entry in expected.items()
                           if name.startswith("rust-toolchain/") and name != "rust-toolchain/.obfuscation-build.json"}
        if stage["outputs"] != delivered_stage:
            raise ValueError("delivered compiler/sysroot differs from its build record")
        binary_name = wrapper_path.name.removesuffix(".provenance.json")
        if wrapper["binary_sha256"] != sha256(wrapper_path.with_name(binary_name)):
            raise ValueError("delivered wrapper differs from its build record")
    return manifest


def verify_archive_sidecar(archive):
    sidecar = archive.parent / (archive.name + ".sha256")
    expected = sidecar.read_text().strip()
    if expected != f"{sha256(archive)}  {archive.name}":
        raise ValueError("archive checksum or sidecar filename differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--archive", type=Path,
                        help="also check the compressed archive against its .sha256 sidecar")
    parser.add_argument("--expected-commit",
                        help="require this exact LLVM source commit")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    if args.archive:
        verify_archive_sidecar(args.archive.resolve())
    manifest = verify_tree(args.package.resolve(),
                           expected_commit=args.expected_commit,
                           allow_dirty=args.allow_dirty)
    print(f"PASS package integrity: {args.package.resolve()}")
    print(f"LLVM source commit: {manifest['llvm_source_commit']}")
    print(f"Rust source commit: {manifest['rust_source_commit']}")


if __name__ == "__main__":
    main()
