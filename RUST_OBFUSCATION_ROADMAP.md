# Rust obfuscation support roadmap

Status: proposed implementation plan, 2026-10-08. This document complements
`OBFUSCATION_ROADMAP.md`; it does not mark any Rust milestone complete.

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

## Current baseline

- `llvm/lib/Passes/PassRegistry.def` registers all seven named new-pass-manager
  passes. Clang inserts its pass sequence in
  `clang/lib/CodeGen/BackendUtil.cpp`; rustc constructs its own `PassBuilder`
  and will not inherit Clang's callbacks.
- `llvm/lib/Transforms/Obfuscation/StringObfuscation.cpp` requires
  `isCString()`. Ordinary Rust `&str` backing arrays can remain plaintext.
- `BogusControlFlow.cpp` and `Flattening.cpp` reject whole functions with
  exception-handling pads or unsupported terminators. Rust `panic=unwind` can
  therefore lose control-flow coverage. Split and constant encoding also need
  Rust unwind-IR review.
- Function selection in `Utils.cpp` uses Clang annotations or exact LLVM
  names. Rust source annotations must not be assumed to produce
  `llvm.global.annotations`; monomorphization and mangling complicate names.
- `.github/workflows/obfuscation.yml` runs Clang/`opt` tests on Linux amd64.
  `.github/workflows/release.yml` builds four LLVM/Clang archives, none of
  which contains rustc, a Rust sysroot, or a Cargo wrapper.

## Milestones

The estimates are engineering time for one engineer, not elapsed CI time.
Re-estimate after milestones 0 and 1, especially if Rust 1.99 cannot link
cleanly against this fork or unwind-aware rewriting needs a larger redesign.

| Milestone | Implementation | Exit gate | Estimate |
| --- | --- | --- | --- |
| **0. Toolchain proof** | Pin the Rust 1.99 and LLVM revisions. Build a stage-1 rustc and sysroot against this fork; build `llvm-config` and ensure `LLVMObfuscation` is linked into rustc. Use `-C passes` only as an initial named-pass probe. | Direct rustc fixtures compile and run on macOS arm64 and Linux amd64. Each named pass parses; eligible test code shows the expected IR and final-artifact effect. Record real Rust O0 behavior, including `optnone`. Wrong LLVM versions and unknown passes fail clearly. | 1 week |
| **1. Stage-aware pipeline** | Put the ordered, opt-in pass sequence in a shared LLVM integration point. First prove whether an LLVM `PassBuilder` hook controlled by rustc's `-C llvm-args` can cover O0, optimized, pre-link, ThinLTO, and fat-LTO stages without double-running Clang's callbacks. If not, add a small pinned Rust `PassWrapper.cpp` callback patch. | A single-crate fixture is transformed once at the intended stage in ordinary and LTO builds. Clang does not double-run the passes. Pass order and stage policy are documented; cross-crate isolation is gated in milestone 5. | 1–2 weeks |
| **2. Cargo and selection** | Add a Cargo-facing wrapper and configuration for package, target, crate type, and optional function/global selection. Use Cargo's workspace wrapper for members and a Cargo-wide wrapper or equivalent interception for explicitly selected external dependencies; filter host build tools by crate type and target. Emit a per-build manifest with raw LLVM names, demangled Rust names, matched/transformed/skipped/unmatched counts, and reasons. Define generic, closure, async, inlined, and multi-CGU selection behavior. | A workspace member and a non-workspace registry dependency can be selected independently; unselected crates, host build scripts, and proc macros stay outside the default policy. Strict unmatched selection fails. `cargo check` reports that it produced no protected code artifact. | 1–2 weeks |
| **3. Rust strings and data** | Extend the string pass to use-aware byte arrays, including non-NUL `&str`, UTF-8, embedded NUL, byte strings, and static/const references. Preserve slice pointer/length values and decoder initialization order. Keep unsafe exported, weak/COMDAT, metadata, and early-initialization cases excluded with reasons. Audit scalar globals and constant encoding against Rust IR without broadening their safety rules indiscriminately. | Selected eligible literals are absent as plaintext from final artifacts and retain exact bytes at runtime. `static`, `const`, FFI, and duplicate-definition tests pass or produce a documented skip. Selected scalar statics and constants show effects where eligible. | 2–3 weeks |
| **4. Unwind and control flow** | Make BCF and flattening operate on eligible normal regions of functions containing `invoke`/EH, while preserving exceptional edges and pads. Audit split and narrow constant-encoding skips only where verified safe. Preserve Rust checked-overflow, `Drop`, `catch_unwind`, and `panic=abort` behavior. | Every pass and the combined pipeline verify and run on panic-abort and panic-unwind fixtures. Include native Windows MSVC funclet EH with `catch_unwind`, observable `Drop`, IR verification, and a real pass effect. Normal regions in unwind functions receive an effect where eligible; untouched EH regions have explicit skip reasons. | 2–4 weeks |
| **5. Optimization and LTO** | Define once-only behavior across codegen units, incremental rebuilds, local ThinLTO, cross-crate ThinLTO, and fat LTO. Preserve crate selection after modules merge or are imported; decide explicitly which transforms run before or after LTO. | Final binaries retain the intended effects for `lto="off"`, `lto=false`, `lto="thin"`, and `lto="fat"`, with one and multiple codegen units. The local ThinLTO witness specifically uses `lto=false`, O2/O3, and multiple codegen units. No unselected dependency or sysroot code is transformed by accident. Fixed-seed parallel builds reproduce the same effects after clean and incremental builds; a different seed changes at least one witness. | 1–2 weeks |
| **6. CI, delivery, and docs** | Add native Rust CI first on Linux amd64 and macOS arm64, then Windows amd64 and Linux arm64. Package a pinned custom Rust toolchain/sysroot and Cargo integration as a separate artifact initially. Document installation, configuration, eligibility, reports, version matching, and limitations. | CI is green on all four hosts. Tests run from extracted deliverables, not only the build tree. Record exact Rust/LLVM revisions, checksums, and final-artifact size, build-time, memory, and runtime comparisons. | 2 weeks |

Milestones 2, 3, and 4 can partly proceed in parallel after the toolchain and
pipeline decisions. The working estimate for the full four-platform effort is
roughly 10–16 engineer-weeks; the unwind and string milestones dominate its
uncertainty. Milestone 0 is the first implementation step and a stop/go gate
for the chosen toolchain architecture.

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
| Cargo | `bin`, `rlib`, `dylib`, `cdylib`, `staticlib`, a selected non-workspace registry dependency, an unselected dependency, a build script, and a proc macro; clean and incremental rebuilds. Run `rlib`/`dylib` through Rust consumers and `cdylib`/`staticlib` through C harnesses. |
| Optimization | Rust O0, O2, and release O3; one and multiple codegen units; `lto="off"`, `lto=false`, `lto="thin"`, and `lto="fat"`. Exercise local ThinLTO at O2/O3 with multiple codegen units; verify fixed-seed parallel and incremental repeatability and a different-seed witness. |
| Platforms | Native Linux amd64/arm64, macOS arm64, and Windows amd64 builds; extracted-artifact smoke on each host before a support claim. |

The regression gate for every milestone is an assertions-enabled build, its
focused Rust tests, and `tests/obfuscation/run_portable.py` (the 11 existing
suites plus the final-code split regression when `--objdump` is supplied).
Keep the four-language HTTPS suite in CI where its dependencies and loopback
sockets are available. Reuse the existing deterministic-seed, skip-reporting,
and final-code tests as patterns.

## Integration and release decisions

1. **Default architecture:** a pinned custom rustc linked to this LLVM fork.
   Stock macOS rustc plugin loading is not a dependable route to a four-host
   product. A standalone plugin remains an optional development path where
   Rust's LLVM linkage permits it.
2. **Pipeline ownership:** prefer an opt-in LLVM-side hook only if milestone 1
   proves its stage placement and no-double-insertion behavior. Otherwise
   carry a minimal Rust 1.99 patch, pinned and tested with the LLVM revision.
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
