# Rust obfuscation support roadmap

Status: implementation and acceptance inventory, updated 2026-10-10. This
document complements `OBFUSCATION_ROADMAP.md`. Milestones 0–6 have working
implementations and native acceptance runners; qualification is revision-specific.
The audit baseline `aee13a36f` passed all four native extracted-archive jobs in
[run 38073707099](https://github.com/sliverarmory/llvm-project/actions/runs/38073707099).
Each later revision is qualified by its own complete regression run.

## Goal and support contract

Provide an opt-in Rust/Cargo workflow for this fork's seven registered LLVM
passes: `obf-string`, `obf-split`, `obf-bcf`, `obf-fla`, `obf-sub`, `obf-const`,
and `obf-global-access`. A requested pass must either produce a verified effect
on eligible code or report precisely why it did not. Preserve program behavior
and LLVM IR validity. An increase in IR size alone is not proof of protection.

The first full-support claim covers Rust 1.99 with this LLVM 23 fork, native
builds on the four current release platforms (Windows amd64, macOS arm64,
Linux amd64, Linux arm64), and selected Cargo binaries and libraries. Cover
`std` and `no_std` fixtures, `bin`, `rlib`, `dylib`, `cdylib`, and `staticlib`
outputs where the platform supports them. Support both panic strategies,
ordinary and optimized builds, multiple codegen units, and Cargo's supported
LTO modes. Dependencies may be explicitly selected; build scripts and proc
macros remain outside the target selection by default. Other Rust targets
require their own qualification before being advertised.

Language support does not mean every pass can transform every Rust construct.
For example, TLS and atomic globals are outside the current global-access
pass's eligibility rules. Such cases must preserve behavior and appear as
explicit skips in the build report. A strict mode must fail when a requested
crate, function, global, or pass never matched or never transformed anything
eligible.

## Current implementation

- All seven named passes are registered in `PassRegistry.def`. The shared
  `PassBuilder` hook owns the opt-in Rust pipeline and prevents duplicate
  insertion through Clang callbacks. See [stage policy](tests/obfuscation/rust/PIPELINE.md).
- Rust string encoding handles eligible local byte arrays, including ordinary
  `&str`, UTF-8 and embedded NUL, while preserving slice lengths and runtime
  bytes. Unsafe storage and initialization cases have explicit skip reasons.
  See [string/data eligibility](tests/obfuscation/rust/STRING_DATA.md).
- Split, BCF and flattening transform eligible ordinary regions inside unwind
  functions while preserving pads and exceptional edges. Constant encoding
  and global-access retain their narrower eligibility rules.
  See [EH coverage](tests/obfuscation/rust/EH_REGIONS.md).
- Cargo configuration resolves packages and selects targets, crate types,
  raw LLVM function/global names and typed constants. Reports include raw and
  demangled symbols and per-target strict coverage; effects in one target do
  not satisfy another selected target. Host build scripts and proc macros
  remain excluded. See [wrapper configuration](tools/rust-obfuscation/cargo-wrapper/README.md).
- The custom compiler/sysroot includes `library/test` and rustdoc. Bootstrap
  and packaging bind source content, external LLVM inputs and output hashes;
  edited extracted source trees and stale/mixed binaries are rejected.
- Native CI runs on PRs, main and the Rust development branch. The release
  workflow reuses the four-host gate and publishes separate LLVM/Clang and
  Rust archives only after extracted-artifact verification succeeds.

## Milestones and acceptance evidence

| Milestone | Implemented behavior | Acceptance runner |
| --- | --- | --- |
| 0. Toolchain proof | Pinned Rust 1.99 and LLVM 23, static obfuscation link closure, named passes, O0/O2 behavior and final-code witnesses. | `run_direct.py`, bootstrap/package provenance tests |
| 1. Stage-aware pipeline | Shared LLVM hook, fixed order, once-only ordinary/local/cross-crate LTO placement, Clang callback isolation. | `test_pipeline.py`, `run_pipeline.py` |
| 2. Cargo and selection | Workspace and registry packages, per-target/type strict effects, symbol diagnostics, honest check/cache reporting, argument forwarding. | `test_cargo.py`, `test_cargo_wrapper.py`, wrapper unit tests |
| 3. Rust strings and data | Non-NUL strings, UTF-8, embedded NUL, descriptors, FFI, weak/duplicate/early-init exclusions, scalar eligibility. | `test_string_data.py` |
| 4. Unwind and control flow | Ordinary regions transform while EH is protected; native MSVC funclets, Drop, checked overflow, unwind and abort. | `run_eh_regions.py`, `../test_eh_regions.py` |
| 5. Optimization and LTO | Off/local/thin/fat LTO, one/multiple CGUs, unselected-crate isolation, incremental reporting and seeded parallel repeatability. | `test_lto.py`, `test_seed_parallel.py`, `test_flatten_loop.py` |
| 6. CI, delivery and docs | Four native hosts, clean extracted archives, build provenance, checksums, Cargo output types, unit/integration/doctests, cost reports and tag publication gates. | `verify_package.py --full --benchmark`, `test_cargo_outputs.py`, `test_cargo_test.py` |

Runner paths are under `tests/obfuscation/rust/` unless qualified. Toolchain
packaging helpers are under `tools/rust-obfuscation/`. A runner's existence
is not evidence of a passing revision: retain terminal CI, the tested archive
hashes and per-host smoke reports for every release.

## Acceptance matrix

Use a bounded pairwise matrix for routine CI and a smaller, explicit release
matrix for each platform. `cargo check` must report that it produced no
protected code artifact. Do not claim coverage from that command, IR growth,
or a successful compile alone. For every positive case, compare an ordinary
build with an obfuscated build, verify emitted IR with this fork's `opt`, run
the executable or a linked consumer for library artifacts, and inspect final
code or data for the requested effect.

| Axis | Required witnesses |
| --- | --- |
| Passes | Each of seven passes alone and a supported combined sequence; negative eligibility and strict-unmatched cases. |
| Rust code | Arithmetic with overflow checks, match/branches, generics and monomorphizations, closures/async, scalar statics, FFI exports, `std` and `no_std`. |
| Strings | ASCII, UTF-8, embedded NUL, non-NUL `&str`, byte strings, `static`/`const` references, duplicate/weak data, and early initialization. Check exact runtime bytes and selected plaintext in final artifacts. |
| Panic | Standalone `panic=abort` programs; `panic=unwind`, `catch_unwind`, and destructors with observable drop order. Include a native Windows MSVC funclet witness. Cargo test harnesses ignore the panic profile setting, so they cannot be the sole abort witness. |
| Cargo | `bin`, `rlib`, `dylib`, `cdylib`, `staticlib`, a selected non-workspace registry dependency, an unselected dependency, a build script, and a proc macro; clean and incremental rebuilds. Run `rlib`/`dylib` through Rust consumers and `cdylib`/`staticlib` through C harnesses; run unit, integration, and documentation tests from the delivered sysroot. |
| Optimization | Rust O0, O2, and release O3; one and multiple codegen units; `lto="off"`, `lto=false`, `lto="thin"`, and `lto="fat"`. Exercise local ThinLTO at O2/O3 with multiple codegen units; verify fixed-seed parallel and incremental repeatability and a different-seed witness. |
| Platforms | Native Linux amd64/arm64, macOS arm64, and Windows amd64 builds; extracted-artifact smoke on each host before a support claim. |

The regression gate for every milestone is an assertions-enabled build, its
focused Rust tests, and every suite registered in
`tests/obfuscation/run_portable.py`, including final-code witnesses with
`--objdump` and strict build-manifest and transform-safety regressions.
Keep the four-language HTTPS suite in CI where its dependencies and loopback
sockets are available. Reuse the existing deterministic-seed, skip-reporting,
and final-code tests as patterns.

## Integration and release decisions

1. **Default architecture:** a pinned custom rustc linked to this LLVM fork.
   Stock macOS rustc plugin loading is not a dependable route to a four-host
   product. A standalone plugin remains an optional development path where
   Rust's LLVM linkage permits it.
2. **Pipeline ownership:** the opt-in LLVM-side hook implements stage placement
   and callback isolation without a Rust source patch; keep both regression
   gates when updating either toolchain revision.
3. **Selection surface:** use an explicit Cargo configuration and build report
   first. An ergonomic Rust source attribute is optional follow-up work; the
   roadmap does not rely on Rust generating Clang annotation metadata.
4. **Release shape:** publish a Rust-specific toolchain artifact only after
   extracted-artifact tests pass on all four hosts. Existing LLVM/Clang
   archives do not establish Rust support.
5. **Reuse boundary:** xollvm is an LLVM 22/23 architecture and EH design
   reference; llvm-obfus is a Cargo wrapper design reference. Check licenses
   before reusing code, especially GPL/AGPL examples for Rust strings.

## Research references

- [Rust 1.99 release notes](https://doc.rust-lang.org/releases.html) and the
  [rustc guide to external LLVM builds](https://rustc-dev-guide.rust-lang.org/building/new-target.html#using-pre-built-llvm).
- Rust's [pass builder implementation](https://github.com/rust-lang/rust/blob/1.99.0/compiler/rustc_llvm/llvm-wrapper/PassWrapper.cpp)
  and [LTO extra-pass handling](https://github.com/rust-lang/rust/blob/1.99.0/compiler/rustc_codegen_llvm/src/back/write.rs).
- [Rust LLVM plugin tracking issue](https://github.com/rust-lang/rust/issues/127577),
  [Cargo wrapper configuration](https://doc.rust-lang.org/cargo/reference/config.html#buildrustc-workspace-wrapper),
  and [Cargo profiles](https://doc.rust-lang.org/cargo/reference/profiles.html).
- [xollvm](https://github.com/und3ath/xollvm) and the
  [llvm-obfus Rust frontend guide](https://github.com/90th/llvm-obfus/blob/main/docs/frontends.md).
