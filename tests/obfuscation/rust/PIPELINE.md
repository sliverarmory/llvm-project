# Rust stage-aware LLVM pipeline (milestone 1)

The pinned Rust 1.99 compiler constructs its own LLVM `PassBuilder`. Enable
this fork's integration with one rustc option:

```sh
rustc -C llvm-args=-rust-obf-pipeline=all program.rs
```

`all` can be replaced with a comma-separated selection from `obf-string`,
`obf-split`, `obf-bcf`, `obf-fla`, `obf-sub`, `obf-const`, and
`obf-global-access`. LLVM rejects unknown names. The selection's input order
does not change the pass order. For example,
`-rust-obf-pipeline=obf-sub,obf-split` schedules split before substitution.
An omitted option registers no Rust obfuscation callbacks. Other pass-specific
options, such as exact function and global selection, retain their existing
meaning. Do not also request a selected pass through Rust's `-C passes`; that
M0 probe appends a separate explicit pass and would run it again.

| rustc optimization stage | Placement |
| --- | --- |
| O0, with or without LTO | String at pre-link pipeline start; the remaining six at pre-link optimizer last. Rust's O0 LTO backend has no matching late callback. |
| O1/O2/O3 without LTO | String at pipeline start; the remaining six at optimizer last. |
| O1/O2/O3 ThinLTO | String at pre-link pipeline start; the remaining six after import in the ThinLTO post-link optimizer-last callback. |
| O1/O2/O3 fat LTO | String at pre-link pipeline start; the remaining six in the full-LTO post-link last callback. |

The fixed order is **string, split, bogus control flow, flattening,
substitution, constant encoding, global access indirection**. String encoding
precedes pre-link simplification and embedded bitcode writing so those stages
do not copy selected plaintext first. The late function and data passes run
after most ordinary optimizations, preserving their effects in final code.
The Thin/Fat pre-link late callbacks are deliberately skipped to avoid
running those passes again after LTO. Rust's `PreLinkNoLTO` mode can also run
the ThinLTO pre-link simplifier while writing embedded bitcode; its subsequent
non-LTO optimization phase is the one that gets the late passes.

For exact Cargo package selection, `rust-obf-cargo` adds
`-rust-obf-prelink-only`. This mode runs the six late passes in the selected
crate's Thin/Fat pre-link optimizer-last callback, before LLVM assigns global
GUIDs and writes bitcode. It skips those passes after LTO imports or merges
modules, where the final app's process-wide LLVM options no longer identify
which dependency supplied each function. In local ThinLTO, Rust builds a
pre-link bitcode pipeline and then an ordinary object-code pipeline in the
same `PassBuilder`; the callback schedules the selected passes in the first
pipeline only. Without an LTO pre-link pipeline, the ordinary optimizer-last
callback runs them once. Rust's `-C passes` appends passes after pre-link GUID
assignment and is unsuitable for passes that add globals under Thin/Fat LTO.

The unqualified `-rust-obf-pipeline` option remains process-wide; use the
Cargo wrapper's package selection and pre-link mode for cross-crate LTO. The
fixed test seed now derives a stream per pass and raw symbol, so parallel CGU
execution order does not change selected bytes. Without a test seed, the
entropy-backed generator is unchanged. External linker-plugin LTO remains
outside this Rust toolchain workflow because its linker process does not
receive the wrapper's package selection.

Clang's existing `-mllvm -sobf`, `-split`, `-bcf`, `-fla`, `-sub`, `-constenc`,
and `-gai` callbacks remain the legacy path. If the new option is supplied to
Clang, it owns pass insertion and suppresses the legacy callbacks for that
compile, including annotation-only callback insertion. This prevents duplicate
passes when both option sets are supplied.

Build `opt` and `clang`, then check the callback policy and order:

```sh
python3 tests/obfuscation/rust/test_pipeline.py \
  --opt build-llvm-project/bin/opt \
  --clang build-llvm-project/bin/clang
```

`pipeline.rs` is the companion runtime fixture for the pinned custom rustc.
`run_pipeline.py` compiles and runs it at O0/O2 with LTO off, thin, and fat,
then checks one transformed pass invocation on its unmangled `pipeline_probe`
symbol and exact output. Run it after building the stage-1 compiler against
the current `LLVMPasses` and `LLVMObfuscation` archives:

```sh
python3 tests/obfuscation/rust/run_pipeline.py \
  --rustc build-rust-1.99/build/<host>/stage1/bin/rustc \
  --work-dir build-llvm-project/rust-m1
```
