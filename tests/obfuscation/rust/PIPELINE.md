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

This is once per selected module's intended LLVM pipeline stage. Multiple
codegen units, separate rustc invocations, and imported or merged cross-crate
code need the selection and LTO policy gates in milestones 2 and 5. In
particular, this option alone is process-wide; do not assume that it isolates
one crate from dependencies or the sysroot during LTO. External linker-plugin
LTO is not qualified: the linker process must receive this LLVM option and
use the corresponding PassBuilder callbacks, which this Rust toolchain
workflow does not arrange.

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
