# Rust obfuscation toolchain bootstrap

This is the pinned native stage-1 compiler bootstrap for
`RUST_OBFUSCATION_ROADMAP.md` in the source checkout. It supports native macOS
arm64, Linux amd64/arm64, and Windows amd64 (MSVC). Run the commands below from
the source checkout; an extracted delivery archive does not contain the build
scripts or LLVM source. The script uses Rust 1.99.0 at commit
`b940084d7eb6a299eb4bfeb8e34901bc051e7ac4` and requires this fork's
LLVM 23.1.3 source to descend from
`d881479b157bad1536ce4d77fd06648f89c9fa64`. The exact Rust release
source archive SHA256 and the digest of its file inventory are in
[`pins.json`](pins.json).

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
checkout, it must be at the exact pinned Rust commit with no tracked changes
or additional untracked files under `compiler/`, `library/`, or `src/`.
Otherwise, `prepare` downloads and verifies the official Rust source archive.
The extracted archive is checked against a pinned file inventory on subsequent
runs: changed, missing, or extra source files fail validation. Generated build
directories, bootstrap configuration, Python caches, and top-level build logs
are excluded. An older extracted tree without an inventory must be compared
with the original verified `--archive` by `prepare` before it is accepted.

## Prepare and build

```sh
python3 tools/rust-obfuscation/bootstrap.py prepare
python3 tools/rust-obfuscation/bootstrap.py check
python3 tools/rust-obfuscation/bootstrap.py build --jobs 6
```

`prepare` writes `bootstrap-obfuscation.toml` inside the Rust source tree;
`check` only validates the existing source and LLVM build. `build` prepares as
needed and runs Rust's stage-1 build for `compiler/rustc`, `library/std`,
`library/proc_macro`, `library/test`, and `src/tools/rustdoc` using that config.
The delivered `libtest` and `rustdoc` support Cargo unit, integration, and
documentation tests. The generated config selects the prebuilt `llvm-config`, disables CI
LLVM and compiler downloads, and builds the LLVM codegen backend with static
LLVM linkage. It is separate from any developer-owned `bootstrap.toml`.

Rust 1.99 still needs the pinned `library/backtrace` submodule when automatic
submodule management is disabled. `prepare` initializes that one submodule for
a Git source checkout and verifies its commit. It also builds any missing LLVM
component archives and the 14 LLVM tools that Rust copies into the stage-1
toolchain. For example, this fork's AArch64/X86 link closure includes
`LLVMMCA` and `LLVMX86TargetMCA`. `check` reports missing prerequisites without
changing them. `build` also asks Ninja to refresh the existing LLVM archives
and tools before linking Rust. It uses a writable Cargo cache at
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
compilation and each pass's runtime behavior still require the extracted
toolchain regression tests.

After the build, the stage-1 compiler is under
`build-rust-1.99/build/<host>/stage1/bin/rustc`; `rustdoc` is beside it.
Bootstrap writes `.obfuscation-build.json` in the stage-1 root after checking
that source and LLVM inputs did not change during the build. The record binds
source content, the LLVM archive/tool/configuration hashes, and every delivered
compiler/sysroot file. If the record is missing or inputs/outputs changed,
bootstrap cleans the stage-1 compiler intermediates before rebuilding. This is
necessary because Cargo may reuse `rustc_llvm` after an external LLVM archive
changes. Preserve the build tree and its LLVM inputs until packaging finishes.

Build the Cargo launcher through its provenance helper:

```sh
python3 tools/rust-obfuscation/provenance.py build-wrapper \
  --target-dir build-llvm-project/cargo-wrapper-target
```

The helper builds locked dependencies for the compiler's native host, obtains
the actual executable from Cargo's artifact output, and puts it at
`build-llvm-project/cargo-wrapper-target/release/rust-obf-cargo` (`.exe` on
Windows). Its adjacent `.provenance.json` binds source and compiler inputs to
that binary. Cargo caches are isolated by those content hashes, so a compiler
driver change with an unchanged version string cannot reuse an older wrapper.
A plain `cargo build` does not create the required build record.

Source content identities survive committing unchanged files after a build.
The records retain the revision and dirty state observed during building;
packaging separately records the current checkout commit and requires matching
build inputs. This permits committing completed work without relabeling stale
compiler binaries as newly built. See [`DELIVERY.md`](DELIVERY.md) for packaging
and the full extracted gate, including `test_cargo_test.py`.

The four-host workflow passed at historical revision `aee13a36ffd9`. Qualify
later revisions through their own complete workflow and extracted smoke
reports, including provenance and Cargo test support.

The preflight regression tests do not modify built LLVM libraries:

```sh
python3 -B -m unittest discover -s tools/rust-obfuscation -p 'test_*.py' -v
```

## Pins and upstream configuration

- [Rust 1.99.0 source tag](https://github.com/rust-lang/rust/tree/1.99.0)
- [Rust 1.99.0 bootstrap settings](https://github.com/rust-lang/rust/blob/1.99.0/bootstrap.example.toml)
- [Rust 1.99.0 LLVM component linking](https://github.com/rust-lang/rust/blob/1.99.0/compiler/rustc_llvm/build.rs)
- [External LLVM guidance](https://rustc-dev-guide.rust-lang.org/building/new-target.html#using-pre-built-llvm)

The script's Git ancestry check permits new commits while retaining the pinned
LLVM baseline. Keep the build records, delivery manifest, archive checksums,
and extracted-fixture results together. These hashes detect stale or changed
inputs; they do not authenticate a publisher.

The hosted builds disable optional LLVM terminfo, zstd, zlib, and libxml2
dependencies. A manual configuration can introduce additional runtime
libraries. Before distributing such a build, verify the complete native
dependency chain on a clean destination host; packaging does not automatically
bundle or prove that dependency chain. Passing on the build host is insufficient
if its system or package-manager libraries satisfy missing dependencies.
