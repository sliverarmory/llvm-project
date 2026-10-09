# Cargo obfuscation wrapper

`rust-obf-cargo` launches Cargo with the pinned Rust 1.99/LLVM 23 compiler and
intercepts each rustc invocation. Package rules resolve through `cargo metadata`
to exact manifest directories, so a workspace member and a registry dependency
with similar crate names cannot be confused. The wrapper adds the milestone-1
LLVM pipeline only to selected target crates. It schedules the late passes
before LTO imports or merges crate modules, preserving package selection in
Thin and fat LTO. Cargo host build scripts and proc macros are excluded. It
writes a JSON manifest from structured LLVM events after Cargo finishes.

Build the wrapper with a host Rust toolchain:

```sh
cargo build --locked --release --manifest-path tools/rust-obfuscation/cargo-wrapper/Cargo.toml
```

Example `obfuscation.json` (use an absolute path to the custom compiler):

```json
{
  "version": 1,
  "rustc": "/absolute/path/to/build-rust-1.99/build/<host>/stage1/bin/rustc",
  "seed": "00112233445566778899aabbccddeeff",
  "strict": true,
  "packages": [
    {
      "name": "my-workspace-lib",
      "source": "workspace",
      "targets": ["my-workspace-lib"],
      "crate_types": ["rlib"],
      "passes": ["obf-sub", "obf-split"],
      "functions": ["exact_llvm_function_name"]
    },
    {
      "name": "adler2",
      "version": "2.0.1",
      "source": "registry",
      "passes": ["obf-sub"]
    }
  ]
}
```

Run it from the Cargo workspace:

```sh
rust-obf-cargo --config obfuscation.json --report rust-obf-report.json -- build --release --locked
```

`build`, `check`, `test`, `run`, and `rustc` are accepted Cargo commands. The
launcher adds an explicit target triple when absent. This separates target
crates from host build dependencies and proc macros. It uses a fresh target
directory for each invocation, so the report cannot mistake a cached artifact
for a newly transformed one. For an incremental build, pass
`--reuse-target-dir /absolute/target/path` before `--`. Reuse is allowed only
when the selected rules, seed, compiler and wrapper binary stamps, exact Cargo
arguments, and workspace lockfile match the first build. A no-op cached build
reports `no-selected-crate-compiled` and fails strict coverage; touching or
changing a selected source makes Cargo compile it again and generates fresh
effect events. If another `RUSTC_WRAPPER` or `RUSTC_WORKSPACE_WRAPPER` is
already set, the launcher stops with an error; it does not silently skip that
wrapper.

The `passes` list selects from the pipeline's fixed order: string, split,
bogus control flow, flattening, substitution, constant encoding, then global
access indirection. Its input order does not change that order. `functions`
and `globals` are exact raw LLVM
names; the report includes their demangled names for inspection. All
monomorphizations, closure bodies, and async state-machine methods matching a
crate-wide rule are considered separately. An exact raw function selector
names one emitted symbol only. Inlined code that no longer has a function at
the pipeline stage cannot be selected by name and is reported as unmatched.
Eligible unnamed private Rust byte arrays receive collision-safe synthetic
LLVM names of the form `.rust.obf.bytes.<module-digest>.<ordinal>` before
`globals` selection and effect reporting. Discover those exact names in a
report for the same pinned compiler, source, optimization, and codegen-unit
configuration before using them as selectors; they are not stable source-level
identifiers. Unsafe or ineligible unnamed globals are not renamed. When a
global pass also has `functions`, strict mode requires an effect event for
each selected function and each selected global. Skipped unnamed globals keep
their printable LLVM `@N` operand slot in the report so separate exclusions
remain distinguishable.
`skipped_symbols` counts distinct skipped kind/name pairs, including protected
EH blocks and module-level skips; `matched_symbols` and `transformed_symbols`
count the pass's selected functions or globals.
`targets` use Cargo target names (hyphens and underscores compare equally).
`crate_types` accepts `bin`, `rlib`, `dylib`, `cdylib`, and `staticlib`. An
empty target or type list accepts all non-host targets in that package.

`obf-global-access` requires nonempty `globals`; `obf-const` requires typed
`constants` such as `i32:0x5a17`. String and global-access passes can share a
`globals` list. When both `functions` and `globals` are set, the Rust string
pass requires every direct runtime reader of a selected global to be among
the selected functions; otherwise it records `function-not-selected` and
preserves that global. Unsafe or unsupported symbols are preserved and appear as
skips, with a reason. `strict: true` exits 2 when a selected package/target,
pass, or exact symbol compiles without a transformation; Cargo failures retain
their own exit code. The JSON report contains each rustc invocation, raw and
demangled effect/skip events, per-pass matched/transformed/skipped counts, and
unmatched selectors. `cargo check` explicitly reports
`no-protected-code-artifact`; it does not claim obfuscation coverage.

The event and target directories sit beside the requested report for audit.
Treat the report as build evidence, not as proof that effects survived final
linking. The focused Cargo regression verifies the emitted bitcode with this
fork's `opt`, runs the executable, and inspects linked code, including a
selected registry dependency.

The LTO gate builds a selected workspace library and a mirrored unselected
library, then checks both in the linked app. It covers O2/O3, one/four codegen
units, `lto="off"`, `lto=false`, ThinLTO, and fat LTO; it also checks the
remaining selected pass families under Thin/Fat LTO, verifies the emitted
bitcode, and exercises an incremental rebuild:

```sh
python3 tests/obfuscation/rust/test_lto.py \
  --wrapper /path/to/rust-obf-cargo \
  --rustc /path/to/custom-rust-1.99/bin/rustc \
  --opt build-llvm-project/bin/opt \
  --objdump build-llvm-project/bin/llvm-objdump \
  --work-dir /tmp/rust-obf-lto
```
