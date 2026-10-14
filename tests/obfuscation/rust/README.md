# Direct Rust milestone-0 witnesses

`run_direct.py` exercises a custom Rust 1.99 compiler linked to this LLVM 23
fork. It is a toolchain and named-pass proof, not the Cargo pipeline, selection,
unwind, LTO, or release test for later roadmap milestones.

The build needs the custom `rustc` and sysroot, this fork's `opt` and
`llvm-objdump`, Python 3.10+, OpenSSL for the local test certificate, and
libcurl 7.85+ development/runtime libraries for `https_client.rs`. The Rust
HTTPS client links to libcurl directly so its build does not download Cargo
dependencies. The local server is reused from `../test_http_programs.py`.

Run the full milestone-0 gate with explicit tool paths:

```sh
python3 tests/obfuscation/rust/run_direct.py \
  --rustc /path/to/custom-rust-1.99/bin/rustc \
  --opt build-llvm-project/bin/opt \
  --objdump build-llvm-project/bin/llvm-objdump \
  --llvm-config build-llvm-project/bin/llvm-config \
  --work-dir build-llvm-project/rust-m0
```

`--live-https` additionally makes baseline and transformed Rust clients fetch
`https://example.com/` with certificate validation. The repeatable local HTTPS
test runs by default: it validates the server certificate, follows a redirect,
parses two HTML bodies, checks exact checksums, and confirms that the same
certificate is rejected without its CA file.

For fixture development with an installed Rust compiler that does not use this
fork, run `--baseline-only` with the same paths. It compiles and runs the three
Rust programs, verifies their emitted IR with `opt`, and exercises local HTTPS.
It deliberately skips all obfuscation claims; the full gate rejects a Rust or
LLVM version mismatch before compiling variants.

For each named pass the full gate compiles the direct fixture at O0 and O2,
checks runtime equivalence against an independent arithmetic oracle, and
verifies emitted IR. At O2 it also requires the pass-specific IR witness and
a final artifact effect: changed machine instructions in the selected exported
function, or absence of the selected C-string plaintext in the binary.
The `obf-global-access` case uses a small separate private-scalar fixture
because the current pass excludes functions with EH personalities. The script
records each O0 effect and whether the Rust O0 target has `optnone` in
`rust-m0-report.json`. The ordinary, non-NUL Rust `&str` case remains a
milestone-3 string-data requirement.

The runner emits LLVM IR and the linked executable in **one rustc invocation**.
The exact private names visible during an independent IR-only compile can be
anonymized during a later link-only compile; an allowlist based on those names
would then select nothing in the final artifact. Selected names are also
derived separately for O0 and O2. On the pinned stage-1 compiler, the private
scalar is `[4 x i8]` at O0 and `i32` at O2, so the current global-access pass
reports `reason=not-integer` at O0. The JSON report keeps selected-symbol skip
diagnostics. This direct probe does not establish stable cross-build names,
once-only pipeline placement, or selection after LTO; those remain milestones
2, 1, and 5.

The runner also requires an unknown pass to fail clearly. When supplied,
`llvm-config` must report LLVM 23 and expose `LLVMObfuscation`. A successful
`opt` run on stock rustc IR does not substitute for the custom rustc gate.
