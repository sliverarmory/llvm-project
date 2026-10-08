# Obfuscation roadmap

This roadmap applies to this fork's LLVM 23.1.3 source tree. It is ordered so
each change can be built and tested before the next one begins. Keep the
transforms opt-in and preserve LLVM IR semantics. Do not
count a larger IR file alone as evidence of effective obfuscation.

## Regression gate for every implementation step

1. Rebuild this fork's `clang` and `opt` with assertions enabled.
2. Run the step's focused tests, including a negative case where appropriate.
3. Run `tests/obfuscation/test_transforms.py`, `test_convergence.py`,
   `test_cfg_convergence.py`, and `test_objc_metadata.py` at both optimization
   levels covered by each script. Run `test_http_programs.py` where its TLS,
   libcurl, and Objective-C dependencies and loopback sockets are available.
4. Verify emitted IR with this fork's `opt`; record any platform limitation.
   Proceed to the next step only after the regression gate passes.

The local build command is `ninja -C build-llvm-project clang opt -j 8`. Each
test script takes `--clang build-llvm-project/bin/clang`,
`--opt build-llvm-project/bin/opt`, and its own `--work-dir` below
`build-llvm-project/`.

## Ordered steps

| Step | Change | Focused acceptance check | Status |
| --- | --- | --- | --- |
| 1 | Make instruction substitution preserve LLVM `undef`/poison semantics when an operand is reused. | IR witnesses for every reused-operand identity, plus normal scalar and vector behavior. | Complete: LLVM 23 build, verifier, and all five test suites passed. |
| 2 | Make random seeding fail on entropy errors, avoid shared mutable generator races, and add a deterministic test seed. | Injected entropy failure, repeatable seeded output, and concurrent-use coverage. | Complete: 5 unit tests, seeded CLI tests, and all five regression suites passed. |
| 3 | Reject invalid `sub_loop`, `split_num`, `bcf_prob`, and `bcf_loop` values as compiler errors. | Negative CLI cases exit nonzero and identify the bad option. | Complete: positive/negative O0/O2 CLI cases, a seeded 99% versus 100% BCF boundary test, and full regression gate passed. |
| 4 | Remove dead substituted operations and cap substitution/BCF growth per function. | Repeated-pass tests show bounded IR size and no dead original operation. | Complete: capped O0/O2 IR and runtime checks, private BCF predicate state, and full regression gate passed. |
| 5 | Match function annotations exactly and expose named new-pass-manager passes through `opt`. | Annotation collision tests and direct `opt -passes=...` effect tests. | Complete: exact annotation cases, five named opt passes, runtime checks, and full gate passed. |
| 6 | Add selection policy and performance reporting: per-function controls, selected strings, explicit skip reasons, and size/time measurements. | Selection tests plus a baseline-versus-obfuscated report from final binaries. | Complete: O0/O2 selection and runtime checks, all five regression suites, and a final-binary report passed. |
| 7 | Add selective integer-constant encoding. | IR semantics, cross-optimization, and binary-pattern checks for selected constants. | Complete: typed `opt`/Clang O0/O2 checks, seeded idempotence, arm64 final-code inspection, and full gate passed. |
| 8 | Add selective global-access indirection with ABI exclusions. | Runtime and verifier tests across supported targets, including LTO. | Complete: O0/O2 and full-LTO runtime, final-code slot checks, four cross-target object formats, and full gate passed. |
| 9 | Expand release smoke tests to every packaged platform. | Each release target compiles and runs the portable transform suite. | Implemented: 11-suite extracted-archive smoke passed locally on macOS arm64, including full LTO; hosted matrix awaits CI execution. |

The global-access pass makes a whole-global decision: if any use escapes or a
direct access is outside the selected functions, it skips that global with a
reason. This deliberately favors a predictable selection boundary over partial
indirection. `-obf-report-skips` also reports names absent from the
`-gai-only-globals` and `-sobf-only-globals` lists. An unmatched
`-obf-only-functions` name is still a possible silent no-op across compilation
units; add a per-build selection manifest before making that a hard error.

Step 9 builds and checks the tools inside each Windows amd64, macOS arm64,
Linux amd64, and Linux arm64 release archive before the release job can run.
The macOS archive includes `libLTO.dylib`, and its extracted-tool smoke gate
requires an LTO link and final-code check. The local zip/unzip rehearsal passed
the portable suite and Clang resource-directory check; the full C, C++,
Objective-C, and Objective-C++ HTTPS integration suite passed separately at
O0/O2. The hosted release matrix has not run against these uncommitted changes.

## Follow-up candidates

| Priority | Candidate | Design and acceptance requirement |
| --- | --- | --- |
| 1 | Selection manifest and cost report | Record matched, transformed, skipped, and absent symbols across all compilation units. Fail a build only when the caller asks for strict selection. Measure final code size and runtime per pass, not just IR growth. |
| 2 | Indirect calls and branches | Start with direct calls to local, non-interposable functions. Define explicit exclusions for `musttail`, `invoke`, funclets, CFI, pointer authentication, and unusual calling conventions. Require O0/O2, exception, and full-LTO final-code checks on each target. |
| 3 | Function outlining or wrapping | Select small internal functions with ordinary ABI only. Check recursion, debug info, unwind behavior, ThinLTO import, and code-size cost before widening eligibility. |
| 4 | Machine-IR obfuscation | Prototype one target at a time after instruction selection, with register and unwind validation. The LLVM IR passes should remain the portable baseline. |
| 5 | String and constant recovery tests | Add known-plaintext and static-pattern recovery experiments against the emitted binaries, since a decoder or two-share table is an obstacle to casual inspection rather than secrecy. |

Step 6's local macOS arm64 report is generated by
`tests/obfuscation/report_size_time.py` using the built LLVM 23 Clang. The
32-million-iteration workload produced the same checksum in both variants.
Combined obfuscation increased the final binary from 33,712 to 50,224 bytes
(1.49x), compile-and-link median from 0.0574 to 0.0621 seconds (1.08x), and
runtime median from 0.1048 to 0.1707 seconds (1.63x). These are indicative
single-host measurements, not release performance thresholds. The machine
report is in `build-llvm-project/performance-report-final/`.

## Research behind the order

- [LLVM's IR language reference](https://llvm.org/docs/LangRef.html#freeze-instruction)
  requires one fixed value when a rewrite reuses an `undef`-dependent operand.
  That makes semantic repair the first step.
- [Obfuscator-LLVM's substitution notes](https://github.com/obfuscator-llvm/obfuscator/wiki/Instructions-Substitution)
  say simple instruction substitutions are readily removed by optimization.
  This is why size alone is not an acceptance measure for new transformations.
- [Hikari's documented passes](https://github.com/HikariObfuscator/Hikari/wiki/Usage)
  include indirect branching, function wrapping, function-call obfuscation, and
  string encryption. These are candidates to evaluate after correctness and
  selection controls are in place; they are not drop-in LLVM 23 patches.
- [YANSOllvm's documented validation](https://github.com/emc2314/YANSOllvm#additional-validation)
  includes deterministic output and an LLVM test-suite pass matrix. Its README
  also calls out exception-handling, concurrency, and pass-specific coverage
  gaps. That informs the reproducibility, skip-policy, and platform gates here.
- [O-MVLL's opaque-constants documentation](https://obfuscator.re/o-mvll/passes/opaque-constants/)
  describes automated recovery of repeated encoding patterns. Step 7 therefore
  claims only that selected immediate constants are less obvious in tested
  binaries, not that they are secret.
- [LLVM's inline-assembly constraints](https://llvm.org/docs/LangRef.html#input-constraints)
  define tied input/output registers, which Step 8 uses as an identity barrier
  around a read-only pointer slot on x86_64 and AArch64. The helper survives
  the tested full and ThinLTO pipelines without a volatile load.
- [LLVM's `invariant.group` rules](https://llvm.org/docs/LangRef.html#invariant-group-metadata)
  tie that metadata to the pointer SSA value. Step 8 skips accesses carrying
  it instead of changing their pointer operands.
