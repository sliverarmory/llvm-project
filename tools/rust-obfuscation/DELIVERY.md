# Rust obfuscation toolchain delivery

This workflow uses a custom **Rust 1.99 stage-1 compiler and sysroot** linked
to this LLVM 23 fork. An ordinary installed `rustc` is not interchangeable.
Each native host needs its own archive. The release gate in the source
checkout's `RUST_OBFUSCATION_ROADMAP.md` requires
extracted-archive tests on Linux amd64, Linux arm64, macOS arm64, and Windows
amd64 before calling the toolchain supported on all four hosts.

## Versions and provenance

The bundled `pins.json` pins Rust source commit
`b940084d7eb6a299eb4bfeb8e34901bc051e7ac4` (Rust 1.99.0), the Rust
source archive SHA256 and pinned file-inventory digest, LLVM version 23.1.3,
and the LLVM baseline commit
`d881479b157bad1536ce4d77fd06648f89c9fa64`. A delivered archive's
`manifest.json` gives the **packaging checkout's LLVM commit**, Rust source commit,
host, `rustc -vV`, `opt --version`, source-dirty flag, and SHA256/size of every
included file. Mandatory compiler and launcher build records bind their source
content and build inputs to the actual binary/sysroot hashes. The manifest
retains each component's build revision separately from the packaging commit.
An unchanged source tree can be committed after building because verification
compares content identities; a changed compiler, wrapper, LLVM archive, or tool
must be rebuilt. Check the packaging commit against the release's expected commit;
the baseline pin alone is not the final implementation revision. The current
stage-1 compiler prints `commit-hash: unknown`, so its `-vV` output alone
cannot establish source provenance. Keep the manifest, archive, and `.sha256`
sidecar together.

Build the compiler as described in bundled `BUILD.md` (source path
`tools/rust-obfuscation/README.md`). Build the launcher
from its locked dependencies, then package the host's stage-1 directory:

```sh
python3 tools/rust-obfuscation/provenance.py build-wrapper \
  --target-dir build-llvm-project/cargo-wrapper-target
python3 tools/rust-obfuscation/package.py \
  --stage1 build-rust-1.99/build/<host>/stage1 \
  --rust-source build-rust-1.99 \
  --wrapper build-llvm-project/cargo-wrapper-target/release/rust-obf-cargo \
  --opt build-llvm-project/bin/opt \
  --objdump build-llvm-project/bin/llvm-objdump \
  --output dist/rust-obfuscation-<host>.tar.gz
```

Use `.zip` for a ZIP archive and `.exe` tool names on Windows. `package.py`
accepts the exact, unmodified pinned Rust Git checkout or the SHA256-verified
release source tree prepared by `bootstrap.py`. Extracted Rust sources must
still match the pinned file inventory, including the absence of extra source
files. It refuses a dirty LLVM
checkout. `--allow-dirty` marks an archive for local development; it is not a
release artifact. The package includes `rust-toolchain/`, the launcher and
LLVM tools under `bin/`, licenses, a manifest, and a self-contained integrity
checker, `rustdoc`, the test harness library, and build records. The launcher
helper writes its actual native executable to the path shown above regardless
of inherited Cargo target configuration. Keep the adjacent launcher record and
stage-1 `.obfuscation-build.json`, along with the recorded local LLVM inputs,
available until packaging completes. `--allow-dirty` does not bypass stale or
missing build provenance.

## Install and verify

Copy the archive and its same-named `.sha256` sidecar to the target host.
Compare the archive hash **before extraction** (`sha256sum -c` on Linux,
`shasum -a 256 -c` on macOS, or `Get-FileHash -Algorithm SHA256` on Windows).
Extract to a chosen directory. Inside the single extracted package root, run:

```sh
python3 verify_integrity.py \
  --archive /absolute/path/to/rust-obfuscation-<host>.tar.gz \
  --expected-commit <exact-release-llvm-commit>
./rust-toolchain/bin/rustc -vV
./bin/opt --version
```

For ZIP, give `--archive` its `.zip` path. The integrity checker compares the
sidecar, every file against `manifest.json`, the expected LLVM commit, and the
clean-source flag, and consistency between build records and delivered files.
Hashes detect accidental changes; get the expected commit
and checksum through a trusted release channel because adjacent files do not
authenticate their publisher. On Windows run `rustc.exe` and `opt.exe` from
the same locations. Keep `rust-toolchain/` intact so rustc can find its sysroot.

The repository's stronger extracted-toolchain smoke gate is:

```sh
python3 tools/rust-obfuscation/verify_package.py \
  --archive /absolute/path/to/archive.tar.gz \
  --work-dir /empty/path/to/smoke-work \
  --expected-commit <exact-release-llvm-commit> \
  --clang /absolute/path/to/clang --full
```

It requires a source checkout containing the test fixtures, a fresh work
directory, and the Cargo fixture's locked registry crates (cached or pass
`--online`). It checks the archive and manifest, runs the extracted tools,
the direct Rust pipeline, pipeline stage and callback isolation, seven-pass
Rust gate, Cargo selection, Cargo unit/integration/documentation tests through
the extracted compiler and rustdoc, Rust byte strings and C staticlib consumer, Rust
unwind/control-flow gate, native and cross-target EH IR, LTO and incremental
coverage, fixed-seed parallel repeatability, and the five Cargo output types
with Rust and C consumers. It also checks an optimized loop under local and
cross-crate LTO. The C compiler passed with `--clang` is used for the IR and C
consumer gates; it must support the native target.
`--allow-dirty` accepts a development archive but does not qualify it for
release. Retain `smoke-report.json` and the tested archive SHA256 per host.

## Select Cargo crates

From a Cargo workspace, create `obfuscation.json` with the installed
compiler's absolute path. For example:

```json
{
  "version": 1,
  "rustc": "/absolute/package/root/rust-toolchain/bin/rustc",
  "strict": true,
  "packages": [
    {
      "name": "my-library",
      "source": "workspace",
      "targets": ["my-library"],
      "crate_types": ["rlib"],
      "passes": ["obf-split", "obf-sub"],
      "functions": ["exact_raw_llvm_function_name"]
    },
    {
      "name": "adler2",
      "version": "2.0.1",
      "source": "registry",
      "passes": ["obf-sub"]
    }
  ]
}
```

```sh
/absolute/package/root/bin/rust-obf-cargo \
  --config obfuscation.json --report rust-obf-report.json \
  -- build --release --locked
```

`seed` is optional. A 32-hex-character seed is useful for reproducible test
builds; omitting it uses the toolchain's normal entropy path. The wrapper
resolves package rules through `cargo metadata` to exact manifests, selects
target crates, and excludes host build scripts and proc macros. It adds the
native host target if no `--target` was passed and creates a fresh target
directory for each invocation. It refuses an existing `RUSTC_WRAPPER` or
`RUSTC_WORKSPACE_WRAPPER` and a user-supplied Cargo target directory. `build`,
`test`, `run`, `rustc`, and `check` are accepted Cargo commands. Bundled
`CARGO-WRAPPER.md` has the full rule schema. `CARGO_OUTPUTS.md` describes the
library and consumer acceptance fixture.

The pass names are `obf-string`, `obf-split`, `obf-bcf`, `obf-fla`, `obf-sub`,
`obf-const`, and `obf-global-access`. The pipeline always runs them in its
documented fixed order, regardless of the JSON list order. `functions` and
`globals` are **exact raw LLVM symbol names**, not Rust source paths or
demangled display names. Generic instances, closures, and async bodies may
have distinct symbols. A function inlined before the selected stage may no
longer exist under its requested name. Use the report's raw and demangled
names to find a stable selector, then rerun the exact build.

## Read the report and linked artifact

The JSON report records each rustc invocation, selected package rule,
`compiled` count, effect and skip events, and per-pass
`matched_symbols`, `transformed_symbols`, `transformed_sites`,
`skipped_symbols`, and unmatched selectors. Read `reason` on skip events;
it identifies why a candidate was preserved. `strict: true` exits 2 if a
selected rule or exact selector compiled without the requested effect; Cargo
errors keep Cargo's exit status. A zero exit with `strict: false` is not a
coverage claim. `cargo check` reports
`coverage_status: no-protected-code-artifact` because it creates no protected
machine-code artifact.

Even with `code_artifact: true`, `strict_passed: true`, and transformed sites,
the report describes LLVM pass activity during compilation. For a release or
security-sensitive build, run the executable (or a C/Rust consumer of a
library), compare its output with an ordinary build, and inspect the final
binary or library for the requested instruction/data effect. Inlining,
linking, and LTO can change what survives. The focused Cargo test checks a
linked selected function in addition to the report.

Native Windows final-code fixtures compile both variants with
`-C force-frame-pointers=yes` so PE unwind metadata can bound each exported
probe. The shared disassembly helper uses those exact ranges and rejects
branching leaf functions whose extent is unavailable. Saved COFF object
witnesses retain their ordinary symbol bounds.

## Eligibility and current limits

- String encoding handles eligible local constant byte arrays, including
  non-NUL Rust strings, UTF-8, and embedded NUL. Exported, weak, metadata-used,
  early-initialization, and unsupported pointer-flow cases have explicit skip
  reasons; see bundled `STRING_DATA.md`.
- BCF and flattening can change eligible ordinary regions in unwind functions
  while preserving EH pads and exceptional edges. Protected EH regions may be
  skipped; see bundled `EH_REGIONS.md`.
- `obf-global-access` needs exact integer global selectors; `obf-const` needs
  typed constants such as `i32:0x5a17`. Neither option promises that every
  source-level value remains an eligible LLVM value after optimization.
- The archive is native to its built host. External linker-plugin LTO is not
  integrated by this launcher. `--full` qualifies cross-crate LTO, multiple
  codegen units, incremental reuse, and final-code repeatability for the
  archive's exact source revision.
- Run the current `--full` gate on a clean archive for each of the four
  native hosts. A development archive or a build-tree test does not establish
  release support. Refer to the exact revision's terminal CI and extracted
  smoke reports before claiming four-host support.
- Native dependency portability depends on the build configuration. Hosted
  builds disable optional LLVM terminfo, zstd, zlib, and libxml2 dependencies.
  Manual builds must verify the complete runtime dependency chain on a clean
  destination host; the packager does not automatically bundle arbitrary
  system or package-manager libraries.

The historical four-host run at `aee13a36ffd9` passed. Each delivered revision
requires its own complete verification, including provenance and Cargo tests;
historical success does not establish support for later changes.

For a bounded cost sample on the existing Rust pipeline fixture, run
`tests/obfuscation/rust/benchmark_rust.py` with `--rustc`, `--objdump`, and
`--work-dir`. It writes `benchmark-report.json` with ordinary and obfuscated
final binary sizes in bytes, compile wall time in nanoseconds, top-level
compiler peak resident memory in bytes, and process-level runtime wall time
in nanoseconds. Windows uses peak working set; Unix uses `ru_maxrss`, so
compare memory within a host rather than across operating systems. The
benchmark verifies exact output and a changed final function.
Its Windows configuration also records the frame-pointer setting used by
both variants for exact final-function evidence.
The generated harness includes the unchanged pipeline fixture and calls its
probe 100,000 times by default. Runtime still includes process launch and
should not be treated as isolated function throughput. Use the JSON's raw
samples and caveats when comparing hosts or revisions.
