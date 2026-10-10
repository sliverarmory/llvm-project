# Cargo output and language acceptance

`test_cargo_outputs.py` builds an ordinary and a selected Cargo workspace with
the pinned Rust 1.99/LLVM 23 compiler. The workspace has no external packages,
so `--locked --offline` works on a fresh host. The selected rules use one
deterministic seed. Each rule must report an effect on its exported probe. The
runner then executes both builds and compares the probe's final linked machine
instructions, rather than treating a Cargo report as final-code proof. It also
verifies the LLVM bitcode saved by those same Cargo builds with this fork's
`opt`.

| Selected target | Consumer and final-code witness |
| --- | --- |
| `bin` | Executable `accept_bin_probe`; it also executes generic `u32` and `u64` instantiations, a captured closure, and an async future. Each Rust witness must have one exact demangled `obf-split` effect event. macOS and Linux compare each final linked symbol; Windows compares each saved COFF function and checks that its selected instructions survive in the final PE code. |
| `rlib` | The library has `#![no_std]`; a Rust executable calls its probe. Final code is checked in that executable, not in the intermediate rlib. |
| `dylib` | A Rust executable calls its probe. Final code is checked in the loaded Rust dynamic library. |
| `cdylib` | A C executable loads and calls its exported probe. Final code is checked in the shared library. |
| `staticlib` | A C executable links and calls its exported probe. Final code is checked in that executable, not in the intermediate archive. |

Run from the repository root with a built Cargo wrapper and custom stage-1
compiler:

```sh
python3 tests/obfuscation/rust/test_cargo_outputs.py \
  --rustc build-rust-1.99/build/<host>/stage1/bin/rustc \
  --wrapper build-llvm-project/cargo-wrapper-target/release/rust-obf-cargo \
  --opt build-llvm-project/bin/opt \
  --objdump build-llvm-project/bin/llvm-objdump \
  --cc clang \
  --work-dir build-llvm-project/rust-output-acceptance
```

The runner sets `-C prefer-dynamic -C save-temps=yes` for both builds. A Rust `dylib`
linked into a Rust executable needs the same dynamic Rust standard library;
the ordinary static-`std` mode fails with `cannot satisfy dependencies so std
only shows up once`. The runner adds the selected target directory and dynamic
sysroot to the platform library search path. The C static library remains a
self-contained archive and is linked directly by the C harness.

A linked Windows PE can omit private Rust names from its symbol table. Rustc's
saved COFF objects retain those names, so the Windows gate compares each named
baseline and selected object function. It then requires each object's opcode
sequence to appear in its corresponding linked executable and the selected
sequence to be absent from the baseline executable. This proves retained code
but does not identify a private PE function by name after linking. The result
JSON labels the evidence as `saved COFF object and linked PE code`.

A missing C driver fails the gate on every host. Any crate type or consumer
that fails to compile, link, run, or retain its final effect fails the gate
with the command and error output. This fixture currently has a native macOS
arm64 pass; Linux amd64/arm64 and Windows amd64 need their own native runs
before a four-platform support claim.
