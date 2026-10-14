"""Bind local build products to their source content and build inputs.

These records prevent accidental stale/mixed packaging; they are not signatures.
Content identities survive committing an unchanged working tree after a build.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from verify_integrity import validate_native_rustc_host


ROOT = Path(__file__).resolve().parents[2]
BUILD_PATHS = (
    "llvm/CMakeLists.txt", "llvm/lib", "llvm/include", "llvm/tools",
    "llvm/utils", "llvm/cmake", "clang/CMakeLists.txt", "clang/lib",
    "clang/include", "clang/tools", "clang/utils", "clang/cmake",
    "cmake", "third-party", "tools/rust-obfuscation/bootstrap.py",
    "tools/rust-obfuscation/provenance.py", "tools/rust-obfuscation/pins.json",
    "tools/rust-obfuscation/verify_integrity.py",
)
WRAPPER_PATHS = ("tools/rust-obfuscation/cargo-wrapper/Cargo.toml",
                 "tools/rust-obfuscation/cargo-wrapper/Cargo.lock",
                 "tools/rust-obfuscation/cargo-wrapper/src")
STAGE_RECORD = ".obfuscation-build.json"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True).stdout


def source_identity(root=ROOT, paths=BUILD_PATHS):
    """Hash index blob IDs plus current edits, including untracked inputs."""
    entries = {}
    for item in git(root, "ls-files", "-s", "-z", "--", *paths).split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        mode, blob, stage = metadata.split()
        if stage != b"0":
            raise ValueError("cannot build with unresolved source conflicts")
        entries[os.fsdecode(name)] = [mode.decode(), blob.decode()]
    changed = git(root, "diff", "--name-only", "-z", "--", *paths)
    added = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths)
    for raw in set((changed + added).split(b"\0")) - {b""}:
        name = os.fsdecode(raw)
        path = root / name
        if not path.exists() and not path.is_symlink():
            entries.pop(name, None)
            continue
        data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        mode = "120000" if path.is_symlink() else ("100755" if path.stat().st_mode & 0o111 else "100644")
        entries[name] = [mode, blob]
    return digest_json(entries)


def file_inventory(paths, root=None):
    result = {}
    for path in paths:
        path = Path(path)
        name = path.relative_to(root).as_posix() if root else str(path.resolve())
        result[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    return result


def stage_files(stage):
    # Match package.ignored_sources before inspecting or descending into source
    # entries. On Windows these contain junctions back to the Rust checkout,
    # which itself contains this stage and would make recursive globbing cycle.
    files = []
    pending = [Path(stage)]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if (directory.name == "rustlib" and
                        entry.name in ("src", "rustc-src")):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.name != STAGE_RECORD and entry.is_file():
                    # Hash file symlink contents, but never follow directory
                    # symlinks, consistent with the previous glob traversal.
                    files.append(Path(entry.path))
    return files


def stage_inventory(stage):
    return file_inventory(stage_files(stage), stage)


def verify_inventory(recorded, paths, root=None):
    if recorded != file_inventory(paths, root):
        raise ValueError("build inputs or outputs changed; rebuild before packaging")


def write_record(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def wrapper_record_path(wrapper):
    return wrapper.with_name(wrapper.name + ".provenance.json")


def verify_wrapper(wrapper, root=ROOT):
    record = json.loads(wrapper_record_path(wrapper).read_text())
    if record.get("schema_version") != 1 or record.get("kind") != "cargo-wrapper":
        raise ValueError("invalid wrapper build provenance")
    if record["source_identity"] != source_identity(root, WRAPPER_PATHS):
        raise ValueError("Cargo wrapper source changed; run provenance.py build-wrapper")
    if record["binary_sha256"] != sha256(wrapper):
        raise ValueError("Cargo wrapper binary differs from its build provenance")
    return record


def verify_stage(stage, tools, root=ROOT):
    record = json.loads((stage / STAGE_RECORD).read_text())
    if record.get("schema_version") != 1 or record.get("kind") != "rust-stage1":
        raise ValueError("invalid stage-1 build provenance")
    if record["source_identity"] != source_identity(root):
        raise ValueError("LLVM/build source changed; rerun bootstrap.py build")
    if record["outputs"] != stage_inventory(stage):
        raise ValueError("stage-1 compiler/sysroot differs from build provenance")
    for tool in tools:
        expected = record["llvm_tools"].get(tool.name)
        if expected != {"bytes": tool.stat().st_size, "sha256": sha256(tool)}:
            raise ValueError(f"LLVM tool differs from stage-1 link inputs: {tool}")
    # Local packaging also checks the archives actually linked into rustc.
    verify_inventory(record["llvm_inputs"], [Path(p) for p in record["llvm_inputs"]])
    return record


def build_wrapper(target_dir, cargo="cargo", root=ROOT):
    before = source_identity(root, WRAPPER_PATHS)
    env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir.resolve()))
    rustc = env.get("RUSTC", "rustc")
    sysroot = Path(subprocess.run([rustc, "--print", "sysroot"], check=True,
                                 capture_output=True, text=True).stdout.strip())
    compiler = sysroot / "bin" / ("rustc.exe" if os.name == "nt" else "rustc")
    # Cargo configuration can otherwise select a different compiler than the
    # one whose input hashes we record here.
    env["RUSTC"] = str(compiler)
    compiler_version = subprocess.run([str(compiler), "-vV"], check=True,
                                      capture_output=True, text=True).stdout.strip()
    host = validate_native_rustc_host(compiler_version)
    compiler_inputs = file_inventory([compiler, Path(shutil.which(cargo) or cargo),
                                     *sorted((sysroot / "lib").glob("*rustc_driver*")),
                                     *sorted((sysroot / "bin").glob("*rustc_driver*"))])
    command = [cargo, "build", "--locked", "--release", "--target", host,
               "--message-format=json", "--manifest-path",
               str(root / "tools/rust-obfuscation/cargo-wrapper/Cargo.toml")]
    # Cargo fingerprints the compiler's version, not the bytes of its shared
    # driver. Keep cached objects isolated by the actual compiler and source
    # contents so an unchanged -vV cannot recertify an older binary.
    build_inputs = {"schema_version": 1, "source_identity": before,
                    "compiler_inputs": compiler_inputs, "command": command}
    build_identity = digest_json(build_inputs)
    # A short directory name avoids MSVC MAX_PATH failures. The full record
    # detects collisions before any existing cache is trusted.
    cache = target_dir.resolve() / "p" / build_identity[:20]
    cache.mkdir(parents=True, exist_ok=True)
    cache_record = cache / ".wrapper-build-inputs.json"
    if cache_record.is_file():
        if json.loads(cache_record.read_text()) != build_inputs:
            raise ValueError("wrapper build cache has a different full input identity")
    elif any(cache.iterdir()):
        raise ValueError("wrapper build cache has no input identity; use a fresh target directory")
    else:
        write_record(cache_record, build_inputs)
    env["CARGO_TARGET_DIR"] = str(cache)
    command.extend(("--target-dir", str(cache)))
    result = subprocess.run(command, env=env, check=True, stdout=subprocess.PIPE, text=True)
    artifacts = []
    for line in result.stdout.splitlines():
        message = json.loads(line)
        if (message.get("reason") == "compiler-artifact" and
                message.get("target", {}).get("name") == "rust-obf-cargo" and
                "bin" in message.get("target", {}).get("kind", []) and
                message.get("executable")):
            artifacts.append(Path(message["executable"]).resolve())
    if len(artifacts) != 1 or not artifacts[0].is_file():
        raise ValueError("Cargo did not identify exactly one built wrapper executable")
    if before != source_identity(root, WRAPPER_PATHS):
        raise ValueError("Cargo wrapper sources changed during build")
    verify_inventory(compiler_inputs, [Path(p) for p in compiler_inputs])
    binary = target_dir.resolve() / "release" / ("rust-obf-cargo.exe" if os.name == "nt" else "rust-obf-cargo")
    binary.parent.mkdir(parents=True, exist_ok=True)
    if artifacts[0] != binary:
        shutil.copy2(artifacts[0], binary)
    record = {"schema_version": 1, "kind": "cargo-wrapper",
              "source_identity": before,
              "build_revision": git(root, "rev-parse", "HEAD").decode().strip(),
              "build_source_dirty": bool(git(root, "status", "--porcelain")),
              "binary_sha256": sha256(binary), "command": command,
              "build_identity": build_identity,
              "compiler_inputs": compiler_inputs,
              "rustc_version": compiler_version,
              "cargo_version": subprocess.run([cargo, "--version"], check=True,
                                               capture_output=True, text=True).stdout.strip()}
    write_record(wrapper_record_path(binary), record)
    print(f"built wrapper with provenance: {binary}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-wrapper")
    build.add_argument("--target-dir", type=Path, required=True)
    build.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    build_wrapper(args.target_dir, args.cargo)


if __name__ == "__main__":
    main()
