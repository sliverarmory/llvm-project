# Rust EH control-flow checks

BCF and flattening now transform eligible ordinary blocks in a function that
also contains unwind handling. They leave invokes, EH pads, funclet bodies,
and landingpad cleanup continuations (including joins shared with normal
flow) in place. A Windows continuation after `catchret` has exited its funclet
and can be transformed as ordinary control flow.
BCF skips a normal block if its branch crosses into a protected region.
Flattening routes each eligible normal conditional branch through its own
volatile state dispatcher; it does not perform the full-function flatten used
when a function has no EH. This keeps source values dominant over successor
PHIs and leaves unwind edges unchanged.

After building LLVM and relinking the pinned Rust 1.99 stage1 compiler:

```sh
python3 tests/obfuscation/rust/run_eh_regions.py \
  --rustc build-rust-1.99/build/aarch64-apple-darwin/stage1/bin/rustc \
  --opt build-llvm-project/bin/opt \
  --work-dir build-llvm-project/rust-m4/rust

python3 tests/obfuscation/test_eh_regions.py \
  --clang build-llvm-project/bin/clang \
  --opt build-llvm-project/bin/opt \
  --work-dir build-llvm-project/rust-m4/clang
```

The Rust test covers O0 and O2 with `panic=unwind` and `panic=abort`. It
compares exact output for successful calls, caught explicit panic, checked
arithmetic overflow, and the count and order of both `Drop` handlers. It
also verifies emitted IR and checks that EH pad counts and invoke unwind
destinations remain unchanged. Under `panic=abort`, an uncaught panic must
exit unsuccessfully.

The Clang test checks native Itanium and cross-target Windows MSVC IR at O0
and O2, including `landingpad`/`catchpad` preservation and actual BCF and
flattening effects. Cross-target Windows compilation cannot exercise the
MSVC runtime or native Rust `Drop` behavior on macOS. This local Rust stage1
has no `x86_64-pc-windows-msvc` target library directory. Run the Rust test on
a Windows MSVC host with this fork's Rust stage1 and target libraries to cover
that final runtime gate; its IR assertions accept Windows `cleanuppad` EH.
