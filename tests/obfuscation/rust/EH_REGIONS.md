# Rust EH pass matrix

The Rust fixture exercises all seven named passes separately and the supported
`-rust-obf-pipeline=all` sequence. It builds a baseline and eight transformed
variants at O0 and O2 for each of `panic=unwind` and `panic=abort`: 36 native
executables in all. The pass event file must show an effect on the selected
symbol, or the exact safety skip expected from the baseline IR. A successful
compile or an unrelated pass effect is not counted as coverage.
For BCF and flattening with unwinding, it also requires distinct, nonempty
`protected-eh-region` block-skip events covering at least every EH pad; abort
builds must not report protected EH blocks.

BCF and flattening transform eligible ordinary blocks in a function that also
contains unwind handling. They leave invokes, EH pads, funclet bodies, and
landingpad cleanup continuations (including joins shared with normal flow) in
place. A Windows continuation after `catchret` has exited its funclet and can
be transformed as ordinary control flow. BCF skips a normal block if its
branch crosses into a protected region. Flattening routes each eligible
normal conditional branch through its own volatile state dispatcher; it does
not perform the full-function flatten used when a function has no EH. This
keeps source values dominant over successor PHIs and leaves unwind edges
unchanged.

After building LLVM and relinking the pinned Rust 1.99 stage1 compiler:

```sh
python3 tests/obfuscation/rust/run_eh_regions.py \
  --rustc build-rust-1.99/build/aarch64-apple-darwin/stage1/bin/rustc \
  --opt build-llvm-project/bin/opt \
  --objdump build-llvm-project/bin/llvm-objdump \
  --work-dir build-llvm-project/rust-m4/rust

python3 tests/obfuscation/test_eh_regions.py \
  --clang build-llvm-project/bin/clang \
  --opt build-llvm-project/bin/opt \
  --work-dir build-llvm-project/rust-m4/clang
```

The selected Rust byte literal is the explicit panic message; the test
requires its plaintext to disappear from both emitted IR and the linked
executable. Split, BCF, flattening, and substitution must report an effect
on `eh_probe` and leave their expected IR witnesses. Constant encoding
selects the `i32:11` arithmetic operand: it must transform `panic=abort`, but
must report `unsupported-control-flow` for the EH function under
`panic=unwind`. The private `EH_STATE` scalar is accessed only inside
`eh_probe`. At O2 with unwinding, global-access indirection must report
`function-abi` rather than insert a helper call in a function with an EH
personality; at O2 with abort it must transform the scalar. Rust currently
represents this scalar as `[4 x i8]` at O0, so that case must report
`not-integer`. The test derives its representation from the baseline IR;
if a target emits `i32` at O0, it expects the corresponding `function-abi`
skip or abort-mode effect.

For every variant, the test verifies emitted IR with `opt`, disassembles the
linked `eh_probe`, compares EH pad and invoke counts and the exact invoke
unwind destinations, and checks exact runtime output for successful calls,
caught explicit panic, checked arithmetic overflow, and the count and order
of both `Drop` handlers. Every variant with a non-string pass effect must
change the linked function's opcode sequence from its baseline. Under
`panic=abort`, every variant's uncaught panic must exit unsuccessfully. The
combined variant must show the appropriate effect or skip for each pass in
the same compiler invocation. On native Windows, the Rust test also requires
the module's MSVC `catchswitch` and `catchpad` regions and preserves their
counts across all variants.

The Clang test checks native Itanium and cross-target Windows MSVC IR at O0
and O2, including `landingpad`/`catchpad` preservation and actual BCF and
flattening effects. Cross-target Windows compilation cannot exercise the
MSVC runtime or native Rust `Drop` behavior on macOS. This local Rust stage1
has no `x86_64-pc-windows-msvc` target library directory. Run the Rust test on
a Windows MSVC host with this fork's Rust stage1 and target libraries to cover
that final runtime gate; its IR assertions accept Windows `cleanuppad` EH.
