# Rust milestone 0 bootstrap

This is the pinned native stage-1 compiler bootstrap for
[`RUST_OBFUSCATION_ROADMAP.md`](../../RUST_OBFUSCATION_ROADMAP.md). It currently
supports macOS arm64 and Linux amd64. The script uses Rust 1.99.0 at commit
`b940084d7eb6a299eb4bfeb8e34901bc051e7ac4` and requires this fork's
LLVM 23.1.3 source to descend from
`d881479b157bad1536ce4d77fd06648f89c9fa64`. The exact Rust release
source archive SHA256 is in [`pins.json`](pins.json).

## Prerequisites

Build an assertions-enabled LLVM tree from this checkout, including `clang`,
`opt`, and `llvm-config`. For the default build directory:

```sh
ninja -C build-llvm-project clang opt llvm-config
```

Use a native Rust host with Python 3.10 or newer, `curl`, `tar`, `ninja`, a C/C++ toolchain, and
enough disk space for a Rust compiler build. The script defaults to
`build-llvm-project/bin/llvm-config` and `build-rust-1.99` at the repository
root; both paths can be overridden. If `build-rust-1.99` already contains a Git
checkout, it must be at the exact pinned Rust commit with no tracked changes.
Otherwise, `prepare` downloads and verifies the official Rust source archive.

## Prepare and build

```sh
python3 tools/rust-obfuscation/bootstrap.py prepare
python3 tools/rust-obfuscation/bootstrap.py check
python3 tools/rust-obfuscation/bootstrap.py build --jobs 6
```

`prepare` writes `bootstrap-obfuscation.toml` inside the Rust source tree;
`check` only validates the existing source and LLVM build. `build` prepares as
needed and runs Rust's `x.py build compiler/rustc library/std library/proc_macro` using that
config. The generated config selects the prebuilt `llvm-config`, disables CI
LLVM and compiler downloads, and builds the LLVM codegen backend with static
LLVM linkage. It is separate from any developer-owned `bootstrap.toml`.

Rust 1.99 still needs the pinned `library/backtrace` submodule when automatic
submodule management is disabled. `prepare` initializes that one submodule for
a Git source checkout and verifies its commit. It also builds any missing LLVM
component archives and the 14 LLVM tools that Rust copies into the stage-1
toolchain. For example, this fork's AArch64/X86 link closure includes
`LLVMMCA` and `LLVMX86TargetMCA`. `check` reports missing prerequisites without
changing them. `build` uses a writable Cargo cache at
`<rust-source>/build/obfuscation-cargo-home` by default; override it with
`--cargo-home` if needed. It does not use the account's default Cargo cache.

`llvm-config` exits with `missing: libLLVM*.a` diagnostics before it can print
the static library list. The preflight recognizes only missing LLVM archives
inside this build's library directory, builds their Ninja targets, then repeats
the full query. Other llvm-config errors remain fatal. On macOS, if LLVM needs
`-lzstd`, the script adds the library directory from this build's CMake cache
to `LIBRARY_PATH` for the Rust link. It falls back to `pkg-config libzstd` when
the cached path is unavailable.

The preflight selects the required components and every available optional
backend exactly as Rust 1.99.0's `compiler/rustc_llvm/build.rs` does. It
queries the static library and system library closure and rejects one lacking
`LLVMPasses` or `LLVMObfuscation`. It also checks the LLVM build's version,
assertions, native backend, CMake source path, and built obfuscation archive.
These checks prove the requested library is on rustc's static link line;
compilation and each pass's runtime behavior still require the roadmap's
milestone 0 regression tests.

After the build, the stage-1 compiler is under
`build-rust-1.99/build/<host>/stage1/bin/rustc`. Record its `-vV` output and
run the direct Rust fixtures and the existing portable obfuscation gate before
claiming milestone 0.

The preflight regression tests do not modify built LLVM libraries:

```sh
python3 -B -m unittest discover -s tools/rust-obfuscation -p test_bootstrap.py -v
```

## Pins and upstream configuration

- [Rust 1.99.0 source tag](https://github.com/rust-lang/rust/tree/1.99.0)
- [Rust 1.99.0 bootstrap settings](https://github.com/rust-lang/rust/blob/1.99.0/bootstrap.example.toml)
- [Rust 1.99.0 LLVM component linking](https://github.com/rust-lang/rust/blob/1.99.0/compiler/rustc_llvm/build.rs)
- [External LLVM guidance](https://rustc-dev-guide.rust-lang.org/building/new-target.html#using-pre-built-llvm)

The script's Git ancestry check permits new commits on the Rust obfuscation
branch while retaining the pinned LLVM baseline. For a reproducible build,
record the actual LLVM branch commit, the generated config, and the LLVM and
Rust compiler hashes alongside fixture results. The script rejects unpinned
tracked edits to the Rust source checkout.
