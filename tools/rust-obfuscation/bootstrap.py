#!/usr/bin/env python3
"""Prepare or build a pinned stage-1 Rust compiler against this LLVM fork.

Milestone 0 supports native macOS arm64, Linux amd64/arm64, and Windows amd64
(MSVC). The source archive is verified before extraction; an existing git
checkout must be at the pinned commit. `check` is read-only, `prepare` writes
a separate bootstrap config, and `build` also starts the long Rust bootstrap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((Path(__file__).with_name("pins.json")).read_text())
REQUIRED_COMPONENTS = (
    "ipo", "bitreader", "bitwriter", "linker", "asmparser", "lto",
    "coverage", "instrumentation",
)
OPTIONAL_COMPONENTS = (
    "x86", "arm", "aarch64", "amdgpu", "avr", "loongarch", "m68k",
    "csky", "mips", "powerpc", "systemz", "webassembly", "msp430",
    "sparc", "nvptx", "hexagon", "riscv", "xtensa", "bpf",
)
# Rust 1.99.0 src/bootstrap/src/lib.rs::LLVM_TOOLS. Bootstrap copies these
# into the stage-1 sysroot while assembling the compiler.
LLVM_TOOLS = (
    "llvm-cov", "llvm-nm", "llvm-objcopy", "llvm-objdump", "llvm-profdata",
    "llvm-readobj", "llvm-size", "llvm-strip", "llvm-ar", "llvm-as",
    "llvm-dis", "llvm-link", "llc", "opt",
)


def run(*command: str | Path, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [str(part) for part in command], cwd=cwd,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(str(part) for part in command)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def host_triple() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "aarch64-apple-darwin"
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-gnu"
    if system == "Linux" and machine in ("arm64", "aarch64"):
        return "aarch64-unknown-linux-gnu"
    if system == "Windows" and machine in ("x86_64", "amd64"):
        return "x86_64-pc-windows-msvc"
    raise RuntimeError(f"no native bootstrap recipe for {system}/{machine}")


def check_msvc_environment(host: str) -> None:
    if not host.endswith("windows-msvc"):
        return
    target_arch = os.environ.get("VSCMD_ARG_TGT_ARCH")
    if target_arch and target_arch.lower() not in ("x64", "amd64"):
        raise RuntimeError(f"Windows MSVC bootstrap needs vcvars64.bat, got {target_arch} target")
    if (not shutil.which("cl.exe") or not shutil.which("link.exe")
            or not os.environ.get("INCLUDE") or not os.environ.get("LIB")):
        raise RuntimeError(
            "Windows MSVC bootstrap needs a Visual Studio x64 developer command prompt "
            "(vcvars64.bat): cl.exe, link.exe, INCLUDE, and LIB must be available"
        )


def check_fork() -> None:
    head = run("git", "rev-parse", "HEAD", cwd=ROOT)
    try:
        run("git", "merge-base", "--is-ancestor", PINS["llvm_base_commit"], head, cwd=ROOT)
    except RuntimeError as exc:
        raise RuntimeError(
            f"LLVM HEAD {head} does not descend from pinned base {PINS['llvm_base_commit']}"
        ) from exc


def rust_llvm_components(llvm_config: Path) -> list[str]:
    available = run(llvm_config, "--components").split()
    required = set(REQUIRED_COMPONENTS)
    missing = (required | {"obfuscation", "passes"}) - set(available)
    if missing:
        raise RuntimeError(f"LLVM build is missing components: {sorted(missing)}")
    selected = required | set(OPTIONAL_COMPONENTS)
    return [component for component in available if component in selected]


def check_llvm(llvm_config: Path, host: str) -> None:
    if not llvm_config.is_file():
        raise RuntimeError(f"build llvm-config first: {llvm_config}")
    version = run(llvm_config, "--version")
    if version != PINS["llvm_version"]:
        raise RuntimeError(f"LLVM version {version}, expected {PINS['llvm_version']}")
    if run(llvm_config, "--assertion-mode") != "ON":
        raise RuntimeError("milestone 0 requires an assertions-enabled LLVM build")
    target = "AArch64" if host.startswith("aarch64-") else "X86"
    if target not in run(llvm_config, "--targets-built").split():
        raise RuntimeError(f"LLVM build does not include native {target} codegen")

    obj_root = Path(run(llvm_config, "--obj-root")).resolve()
    cache = obj_root / "CMakeCache.txt"
    if not cache.is_file():
        raise RuntimeError(f"cannot prove LLVM build source without {cache}")
    expected_source = (ROOT / "llvm").resolve()
    source_lines = [line.partition("=")[2] for line in cache.read_text().splitlines()
                    if line.startswith("CMAKE_HOME_DIRECTORY:INTERNAL=")]
    # CMake uses forward slashes in its cache even when Python uses Windows paths.
    if len(source_lines) != 1 or Path(source_lines[0]).resolve() != expected_source:
        raise RuntimeError(f"{llvm_config} was not built from {expected_source}")


def llvm_archive_target(path: Path) -> str | None:
    """Return the Ninja target for a native static LLVM archive, if any."""
    if platform.system() == "Windows":
        if path.suffix.lower() == ".lib" and path.stem.startswith("LLVM"):
            return path.stem
    elif path.suffix == ".a" and path.stem.startswith("libLLVM"):
        return path.stem.removeprefix("lib")
    return None


def ensure_llvm_libraries(llvm_config: Path, *, build: bool) -> None:
    # Match rustc_llvm/build.rs: select every available optional backend,
    # query static LLVM libraries plus system dependencies, and quote paths.
    components = rust_llvm_components(llvm_config)
    libdir = Path(run(llvm_config, "--libdir")).resolve()
    obj_root = Path(run(llvm_config, "--obj-root"))
    query = (llvm_config, "--link-static", "--libfiles", *components, "--quote-paths")
    for attempt in range(2):
        try:
            libfiles = shlex.split(run(*query))
            missing = [Path(path) for path in libfiles if not Path(path).is_file()]
        except RuntimeError as exc:
            # llvm-config checks archive existence before printing --libfiles.
            # It reports *all* absent component archives on stderr, one per line.
            reported = re.findall(r"llvm-config: error: missing: ([^\r\n]+)", str(exc))
            if not reported:
                raise
            missing = [Path(path).resolve() for path in reported]
        if not missing:
            break
        for path in missing:
            if path.parent != libdir or not llvm_archive_target(path):
                raise RuntimeError(f"unexpected llvm-config missing library: {path}")
        if not build or attempt:
            raise RuntimeError("Rust LLVM link libraries missing: " + ", ".join(map(str, missing)))
        targets = sorted({target for path in missing if (target := llvm_archive_target(path))})
        print(f"building Rust LLVM link libraries: {' '.join(targets)}", flush=True)
        subprocess.run(["ninja", "-C", str(obj_root), *targets], check=True)
    else:
        raise RuntimeError("LLVM static library query did not converge")

    libs = shlex.split(run(
        llvm_config, "--link-static", "--libs", "--system-libs",
        *components, "--quote-paths"
    ))
    lib_names = {name for lib in libs if (name := llvm_archive_target(Path(lib)))}
    lib_names.update(lib.removeprefix("-l") for lib in libs if lib.startswith("-l"))
    missing_libs = {"LLVMPasses", "LLVMObfuscation"} - lib_names
    if missing_libs:
        raise RuntimeError(f"Rust LLVM static link closure omits: {sorted(missing_libs)}")


def darwin_system_library_paths(llvm_config: Path, host: str) -> list[Path]:
    if not host.endswith("apple-darwin"):
        return []
    components = rust_llvm_components(llvm_config)
    system_libs = shlex.split(run(
        llvm_config, "--link-static", "--system-libs", *components, "--quote-paths"
    ))
    if "-lzstd" not in system_libs:
        return []

    # llvm-config emits -lzstd but not the Homebrew -L path. Prefer the exact
    # library chosen by this LLVM CMake build; pkg-config is a fallback.
    obj_root = Path(run(llvm_config, "--obj-root"))
    cache = obj_root / "CMakeCache.txt"
    candidates = []
    for line in cache.read_text().splitlines():
        if line.startswith("zstd_LIBRARY:FILEPATH="):
            candidates.append(Path(line.split("=", 1)[1]).parent)
            break
    try:
        candidates.append(Path(run("pkg-config", "--variable=libdir", "libzstd")))
    except (OSError, RuntimeError):
        pass
    for candidate in candidates:
        if (candidate / "libzstd.dylib").is_file() or (candidate / "libzstd.a").is_file():
            return [candidate.resolve()]
    raise RuntimeError("LLVM needs -lzstd but neither CMake nor pkg-config found its library directory")


def windows_system_library_paths(llvm_config: Path, host: str) -> list[Path]:
    if not host.endswith("windows-msvc"):
        return []
    components = rust_llvm_components(llvm_config)
    system_libs = shlex.split(run(
        llvm_config, "--link-static", "--system-libs", *components, "--quote-paths"
    ))
    search_paths = []
    for lib in system_libs:
        path = Path(lib)
        if path.is_absolute() and path.suffix.lower() == ".lib":
            if not path.is_file():
                raise RuntimeError(f"LLVM system import library is missing: {path}")
            search_paths.append(path.parent.resolve())

    # rustc_llvm/build.rs converts full MSVC import-library paths to bare
    # rustc-link-lib names. Pass the original directories to the MSVC linker.
    if "zstd.lib" in system_libs and not any((path / "zstd.lib").is_file() for path in search_paths):
        cache = Path(run(llvm_config, "--obj-root")) / "CMakeCache.txt"
        for line in cache.read_text().splitlines():
            if line.startswith("zstd_LIBRARY:FILEPATH="):
                path = Path(line.split("=", 1)[1])
                if path.is_file():
                    search_paths.append(path.parent.resolve())
                break
        if not any((path / "zstd.lib").is_file() for path in search_paths):
            raise RuntimeError("LLVM needs zstd.lib but its CMake library path is unavailable")
    return list(dict.fromkeys(search_paths))


def missing_llvm_tools(llvm_config: Path) -> tuple[Path, list[str]]:
    bindir = Path(run(llvm_config, "--bindir"))
    extension = ".exe" if platform.system() == "Windows" else ""
    return bindir, [name for name in LLVM_TOOLS if not (bindir / f"{name}{extension}").is_file()]


def ensure_llvm_tools(llvm_config: Path, *, build: bool) -> None:
    bindir, missing = missing_llvm_tools(llvm_config)
    if missing and build:
        obj_root = Path(run(llvm_config, "--obj-root"))
        print(f"building Rust bootstrap LLVM tools: {' '.join(missing)}", flush=True)
        subprocess.run(["ninja", "-C", str(obj_root), *missing], check=True)
        _, missing = missing_llvm_tools(llvm_config)
    if missing:
        raise RuntimeError(
            f"Rust bootstrap LLVM tools missing in {bindir}: {', '.join(missing)}; "
            f"run `ninja -C {run(llvm_config, '--obj-root')} {' '.join(missing)}`"
        )


def archive_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_rust_source(source: Path) -> None:
    if not (source / "bootstrap.example.toml").is_file():
        raise RuntimeError(f"not a Rust source tree: {source}")
    if (source / ".git").exists():
        actual = run("git", "rev-parse", "HEAD", cwd=source)
        if actual != PINS["rust_commit"]:
            raise RuntimeError(f"Rust source is {actual}; expected {PINS['rust_commit']}")
        if run("git", "status", "--porcelain", "--untracked-files=no", cwd=source):
            raise RuntimeError("Rust source has tracked modifications; pin a patch before building")
    else:
        marker = source / ".rust-source-sha256"
        if not marker.is_file() or marker.read_text().strip() != PINS["rust_source_sha256"]:
            raise RuntimeError(f"unverified Rust source tree: {source}")
    if (source / "src" / "version").read_text().strip() != PINS["rust_version"]:
        raise RuntimeError(f"Rust source version is not {PINS['rust_version']}")


def ensure_backtrace(source: Path, *, update: bool) -> None:
    backtrace = source / "library" / "backtrace"
    if (source / ".git").exists():
        expected = run("git", "rev-parse", "HEAD:library/backtrace", cwd=source)
        actual = None
        if (backtrace / "Cargo.toml").is_file():
            actual = run("git", "rev-parse", "HEAD", cwd=backtrace)
        if actual != expected and update:
            print("initializing pinned library/backtrace submodule", flush=True)
            subprocess.run(
                ["git", "submodule", "update", "--init", "--depth", "1", "library/backtrace"],
                cwd=source, check=True,
            )
            actual = run("git", "rev-parse", "HEAD", cwd=backtrace)
        if actual != expected:
            raise RuntimeError(
                f"library/backtrace is {actual or 'uninitialized'}, expected {expected}; "
                "run `git submodule update --init --depth 1 library/backtrace`"
            )
    elif not (backtrace / "Cargo.toml").is_file():
        raise RuntimeError("verified release source lacks required library/backtrace")


def fetch_rust_source(source: Path, archive: Path) -> None:
    if source.exists():
        check_rust_source(source)
        return
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists() and archive_sha256(archive) != PINS["rust_source_sha256"]:
        raise RuntimeError(f"cached Rust source archive has the wrong SHA256: {archive}")
    if not archive.exists():
        url = f"https://static.rust-lang.org/dist/rustc-{PINS['rust_version']}-src.tar.xz"
        partial = archive.with_name(archive.name + ".partial")
        subprocess.run(
            ["curl", "--fail", "--location", "--retry", "3", "--output", str(partial), url],
            check=True,
        )
        if archive_sha256(partial) != PINS["rust_source_sha256"]:
            partial.unlink()
            raise RuntimeError("downloaded Rust source archive failed SHA256 verification")
        partial.replace(archive)

    source.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rust-1.99-extract-", dir=source.parent) as temp:
        temp_path = Path(temp)
        subprocess.run(["tar", "-xJf", str(archive), "-C", str(temp_path)], check=True)
        extracted = temp_path / f"rustc-{PINS['rust_version']}-src"
        if not extracted.is_dir():
            raise RuntimeError(f"Rust source archive had no {extracted.name} directory")
        (extracted / ".rust-source-sha256").write_text(PINS["rust_source_sha256"] + "\n")
        extracted.rename(source)
    check_rust_source(source)


def render_config(llvm_config: Path, jobs: int, host: str) -> str:
    if jobs < 1:
        raise RuntimeError("--jobs must be positive")
    # JSON strings are also valid TOML strings; this escapes paths safely.
    llvm_path = json.dumps(str(llvm_config.resolve()))
    return f'''# Generated by tools/rust-obfuscation/bootstrap.py. Do not hand-edit.
change-id = "ignore"

[build]
submodules = false
build-stage = 1
jobs = {jobs}

[llvm]
download-ci-llvm = false
link-shared = false
assertions = true

[rust]
download-rustc = false
codegen-backends = ["llvm"]
channel = "dev"
lld = false

[target.{host}]
llvm-config = {llvm_path}
llvm-has-rust-patches = false
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "prepare", "build"))
    parser.add_argument("--rust-source", type=Path, default=ROOT / "build-rust-1.99")
    llvm_config_name = "llvm-config.exe" if platform.system() == "Windows" else "llvm-config"
    parser.add_argument("--llvm-config", type=Path, default=ROOT / "build-llvm-project/bin" / llvm_config_name)
    parser.add_argument("--archive", type=Path, default=ROOT / "build-rust-1.99-src.tar.xz")
    parser.add_argument("--cargo-home", type=Path,
                        help="writable Cargo cache (default: <rust-source>/build/obfuscation-cargo-home)")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    args = parser.parse_args()
    try:
        host = host_triple()
        if args.action != "check":
            check_msvc_environment(host)
        check_fork()
        llvm_config = args.llvm_config.resolve()
        check_llvm(llvm_config, host)
        ensure_llvm_libraries(llvm_config, build=args.action != "check")
        ensure_llvm_tools(llvm_config, build=args.action != "check")
        if host.endswith("windows-msvc"):
            system_search_paths = windows_system_library_paths(llvm_config, host)
            system_search_variable = "LIB"
        else:
            system_search_paths = darwin_system_library_paths(llvm_config, host)
            system_search_variable = "LIBRARY_PATH"
        source = args.rust_source.resolve()
        if args.action != "check":
            fetch_rust_source(source, args.archive.resolve())
        else:
            check_rust_source(source)
        ensure_backtrace(source, update=args.action != "check")
        cargo_home = (args.cargo_home or source / "build/obfuscation-cargo-home").resolve()
        if args.action != "check":
            cargo_home.mkdir(parents=True, exist_ok=True)
        config = render_config(llvm_config, args.jobs, host)
        config_path = source / "bootstrap-obfuscation.toml"
        if args.action != "check":
            config_path.write_text(config)
        print(f"Rust {PINS['rust_version']} ({PINS['rust_commit']})")
        print(f"LLVM {PINS['llvm_version']} (base {PINS['llvm_base_commit']})")
        print(f"static LLVM link closure contains LLVMPasses and LLVMObfuscation")
        if args.action == "check":
            print(f"bootstrap config would be: {config_path}")
        else:
            print(f"bootstrap config: {config_path}")
        print(f"Cargo home: {cargo_home}")
        if system_search_paths:
            print(f"LLVM system library search: {os.pathsep.join(map(str, system_search_paths))}")
        if args.action == "build":
            env = os.environ.copy()
            env["CARGO_HOME"] = str(cargo_home)
            if system_search_paths:
                existing = env.get(system_search_variable, "")
                env[system_search_variable] = os.pathsep.join(
                    [*(str(path) for path in system_search_paths), *([existing] if existing else [])]
                )
                print(f"{system_search_variable}={env[system_search_variable]}", flush=True)
            subprocess.run(
                [sys.executable, "x.py", "--config", str(config_path), "--stage", "1",
                 "build", "compiler/rustc", "library/std", "library/proc_macro"],
                cwd=source, env=env, check=True,
            )
            print("stage-1 Rust bootstrap completed")
        return 0
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
