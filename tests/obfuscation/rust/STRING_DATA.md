# Rust string and byte-array coverage

The Rust PassBuilder hook runs `obf-string` before the ordinary optimization
pipeline. It enables the byte-array rules for Rust. Direct `opt` tests enable
the same rules with `-sobf-rust-bytes`; the legacy Clang path continues to
encode only NUL-terminated C strings.

An eligible allocation is a nonempty, local, constant `[N x i8]` initializer.
It may contain UTF-8, embedded NUL, or arbitrary bytes and need not end in
NUL. The pass follows users through constant pointer/length descriptors and
ordinary pointer operations. Every runtime user must belong to a selected
function when its use is visible in this graph. Pointer flow through stack
spills or external calls is not tracked for function selection; those reads
still occur after the global decoder. The pass replaces the allocation with
a same-type mutable global, leaves
the descriptor and its length unchanged, and registers a decoder at global
constructor priority zero. The encoded global has `!obf.sobf` metadata to
prevent a second pass invocation from encoding it again. Rust decoder loads
and stores are volatile so later LLVM optimization cannot evaluate the decoder
and restore plaintext to the final initializer.

The pass reports skips with `-obf-report-skips` and writes structured effect
and skip events when `RUST_OBF_EVENT_FILE` is set. Relevant reasons include:

| Reason | Excluded case |
| --- | --- |
| `weak-or-comdat`, `non-local` | Duplicate/weak data or externally visible definitions; separate decoders could disagree after linking. |
| `unsafe-global-user`, `metadata`, `llvm-metadata` | An exported descriptor, `llvm.used`, or other metadata requires stable pre-constructor data. |
| `early-initialization`, `early-ctor-indirect-call` | A priority-zero constructor can read the bytes before the decoder; indirect calls from such a constructor conservatively exclude the allocation. |
| `function-not-selected`, `no-runtime-use` | The allocation crosses the function selection boundary or has no runtime reader. |
| `explicit-section`, `thread-local`, `address-space`, `external-init` | Storage has placement or initialization constraints. |
| `unsupported-constant-use`, `alias-or-ifunc`, `pointer-escapes` | A constant address calculation, alias, or unsupported pointer operation prevents a safe rewrite. |

`string_data.ll` checks ASCII, UTF-8, embedded NUL, arbitrary bytes, and
Rust-style static/const descriptors. `string_data_skips.ll` covers exported,
weak, metadata, early-initialization, mixed-selection, and other exclusions.
`string_data_indirect_ctor.ll` checks the conservative priority-zero indirect
call exclusion. The focused test also runs the pass twice to verify that it
does not register a second decoder.
`string_data.rs` verifies the exact bytes in an executable; the FFI staticlib
fixture is consumed by `string_data_ffi.c`. The test checks that each selected
plaintext marker appears in a baseline final artifact and is absent from the
obfuscated artifact. The C harness holds its expected bytes as numeric values
and the archive is checked before linking the harness.

Run the portable test with:

```sh
python3 tests/obfuscation/rust/test_string_data.py \
  --opt build-llvm-project/bin/opt --clang build-llvm-project/bin/clang \
  --work-dir /tmp/rust-string-data
```

Add `--rustc /path/to/rustc-1.99` to also exercise the pinned Rust 1.99/LLVM 23
toolchain and the C staticlib consumer. The direct Rust fixtures use O0 and O2
and emit IR and the linked artifact in one rustc invocation.
