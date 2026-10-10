# Clang build selection manifest

`clang_obf.py` collects structured compiler events across parallel translation
units. Finish the session after the build completes to report matched,
transformed, skipped, absent, and untransformed selections. Strict selection
is checked across the complete build: a function defined in one translation
unit need not occur in every source file. This complements the Cargo wrapper,
whose package rules require effects in each selected Cargo target.

Create a configuration with exact raw LLVM symbol names:

```json
{
  "version": 1,
  "strict": true,
  "passes": ["obf-sub", "obf-string"],
  "functions": ["selected_function"],
  "string_globals": ["selected_message"],
  "seed": "00112233445566778899aabbccddeeff"
}
```

The seven registered `obf-*` pass names are supported. `functions` selects
function transforms and global-access readers. Clang string selection uses
`string_globals` independently of `functions`. `obf-global-access` requires
`access_globals`; `obf-const` requires typed `constants`, for example
`["i32:0x5a17"]`. The optional seed is for reproducible testing. Empty symbol
lists allow eligible candidates; strict mode still requires an effect from
each requested pass. Compiler annotations retain their ordinary meaning.

Start a new, previously nonexistent session directory for every build:

```sh
python3 /source/tools/clang-obfuscation/clang_obf.py start \
  --config obfuscation.json --session /absolute/build/effects
python3 /source/tools/clang-obfuscation/clang_obf.py compile \
  --session /absolute/build/effects -- /absolute/toolchain/bin/clang \
  -O2 -c first.c -o first.o
python3 /source/tools/clang-obfuscation/clang_obf.py compile \
  --session /absolute/build/effects -- /absolute/toolchain/bin/clang \
  -O2 -c second.c -o second.o
/absolute/toolchain/bin/clang first.o second.o -o program
python3 /source/tools/clang-obfuscation/clang_obf.py finish \
  --session /absolute/build/effects
```

Propagate the compiler, linker, and `finish` exit statuses in your build
script. The launcher forwards the compiler's status. `finish` exits 2 for
unfulfilled strict selections, 1 for failed or interrupted compiler commands,
and 0 otherwise. A non-strict empty session records `no-compiler-events`,
never fresh transformation coverage. An existing finished session cannot
accept more compiler invocations. Retain `report.json` and its invocation
shards with the build artifacts.

For CMake/Ninja, set both `CMAKE_C_COMPILER_LAUNCHER` and
`CMAKE_CXX_COMPILER_LAUNCHER` to this semicolon-separated launcher list:

```text
python3;/source/tools/clang-obfuscation/clang_obf.py;compile;--session;/absolute/build/effects;--
```

Start the session before running the build, then finish it after all compiler
jobs exit. A cached build emits no fresh events; use a clean rebuild when
strict coverage is required. Use this launcher with Clang's ordinary GNU-style
driver interface. Compilation and linking in one driver command, including
multiple source files, is supported. Each cc1 process gets an independent
event file, and the report retains invocation and process identity so local
symbols with the same name are not collapsed in the counts.

Exact selectors are build-wide rather than a requirement on every translation
unit. A missing name is absent only when it appears in no eligible event;
an existing name with only skip events is untransformed. Both fail strict
mode. Skip events preserve their reasons, raw names, and demangled names.
The manifest records compiler activity, not final-binary protection. Verify
program behavior and inspect final machine code/data separately, particularly
after LTO. External linker-plugin invocations must receive the same policy;
this launcher does not inject options into a separate linker process.

The underlying compiler supports `OBF_EVENT_DIR` (one file per process),
`OBF_EVENT_FILE` (one file per invocation), and the existing Cargo
`RUST_OBF_EVENT_FILE`. An unwritable requested event destination fails the
compile rather than silently losing coverage evidence.
