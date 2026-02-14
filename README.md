# LLVM Obfuscator (LLVM v21)

### Clang-exposed transforms

- `-mllvm -sobf`: string obfuscation
- `-mllvm -sub`: instruction substitution
- `-mllvm -sub_loop=<N>`: substitution iteration count (default `1`)
- `-mllvm -split`: basic block splitting
- `-mllvm -split_num=<N>`: splits per block (default `2`, valid `2..10`)
- `-mllvm -bcf`: bogus control flow
- `-mllvm -bcf_prob=<N>`: block obfuscation probability in `%` (default `30`, valid `1..100`)
- `-mllvm -bcf_loop=<N>`: BCF loop count (default `1`, valid `>0`)
- `-mllvm -fla`: control-flow flattening

`split`, `bcf`, and `fla` are now wired into Clang's new PM pipeline:

- `O0`: pipeline-start extension point
- `O1+`: optimizer-last extension point

## Build

From `/Users/moloch/git/llvm-obfuscator`:

```bash
cmake -S llvm-project/llvm -B build-llvm-project \
  -DLLVM_ENABLE_PROJECTS=clang \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build-llvm-project --target clang opt -j8
```

## Usage

Compiler path used below:

```bash
CLANG=/Users/moloch/git/llvm-obfuscator/build-llvm-project/bin/clang
```

### macOS note

If standard headers are missing, pass SDK sysroot:

```bash
SDKROOT=$(xcrun --show-sdk-path)
```

and add `-isysroot "$SDKROOT"` to commands.

### Example commands

```bash
# string obfuscation
$CLANG -isysroot "$SDKROOT" -O2 -mllvm -sobf hello.c -o hello.obf

# substitution
$CLANG -isysroot "$SDKROOT" -O2 -mllvm -sub -mllvm -sub_loop=2 hello.c -o hello.sub

# split / bcf / flattening
$CLANG -isysroot "$SDKROOT" -O2 -mllvm -split hello_complex.c -o hello_complex.split
$CLANG -isysroot "$SDKROOT" -O2 -mllvm -bcf hello_complex.c -o hello_complex.bcf
$CLANG -isysroot "$SDKROOT" -O2 -mllvm -fla hello_complex.c -o hello_complex.fla

# combined control-flow obfuscation
$CLANG -isysroot "$SDKROOT" -O2 \
  -mllvm -split -mllvm -bcf -mllvm -fla \
  hello_complex.c -o hello_complex.all
```

## Function-Level Annotations

These passes use annotations from `llvm.global.annotations`:

```c
__attribute__((annotate("sub")))   int f(int x) { ... }
__attribute__((annotate("nosub"))) int g(int x) { ... }

__attribute__((annotate("split")))   int h(int x) { ... }
__attribute__((annotate("nosplit"))) int i(int x) { ... }

__attribute__((annotate("bcf")))   int j(int x) { ... }
__attribute__((annotate("nobcf"))) int k(int x) { ... }

__attribute__((annotate("fla")))   int m(int x) { ... }
__attribute__((annotate("nofla"))) int n(int x) { ... }
```

Behavior:

- If a pass flag is enabled (`-mllvm -sub`, `-mllvm -split`, `-mllvm -bcf`, `-mllvm -fla`), it applies broadly to eligible functions unless `no<pass>` is present.
- If a flag is not enabled, a positive annotation (`sub`, `split`, `bcf`, `fla`) still enables that pass for the function.

## Quick Verification

### Runtime checks

```bash
./hello.obf
./hello_complex.split
./hello_complex.bcf
./hello_complex.fla
./hello_complex.all
```

Expected sample output:

- `hello.obf`: `Hello from ported LLVM obfuscation: 42`
- `hello_complex.*`: `complex result: 56`

### IR checks

```bash
$CLANG -isysroot "$SDKROOT" -O2 -S -emit-llvm hello_complex.c -o hello_complex.base.ll
$CLANG -isysroot "$SDKROOT" -O2 -S -emit-llvm -mllvm -split hello_complex.c -o hello_complex.split.ll
$CLANG -isysroot "$SDKROOT" -O2 -S -emit-llvm -mllvm -bcf hello_complex.c -o hello_complex.bcf.ll
$CLANG -isysroot "$SDKROOT" -O2 -S -emit-llvm -mllvm -fla hello_complex.c -o hello_complex.fla.ll
```

What to look for:

- `split`: noticeably more basic blocks / `br` instructions vs baseline.
- `bcf`: inserted opaque-predicate globals like `@x` and `@y`.
- `fla`: dispatcher-style flattened CFG with extra state/switch structure.

### String obfuscation sanity check

```bash
strings ./hello.obf | grep "Hello from ported LLVM obfuscation" || true
```

Expected: no plaintext match.

## Implementation Notes

### String obfuscation

- obfuscates constant C-string globals (`ConstantDataSequential::isCString()`)
- skips metadata and ObjC method-name sections
- uses per-byte rolling mask in decode
- emits decode startup via `@llvm.global_ctors`

### Substitution

- integer/int-vector substitutions for add/sub/and/or/xor/mul
- skips add/sub/mul with `nsw`/`nuw` to avoid semantic drift

### Control-flow transforms

- `split`: random split points per block
- `bcf`: inserts opaque predicates and altered blocks
- `fla`: rewrites CFG into dispatcher loop/switch form

## Troubleshooting

- `fatal error: 'stdio.h' file not found`:
  - use `-isysroot "$(xcrun --show-sdk-path)"` on macOS
- very long compile times with heavy obfuscation:
  - reduce `-bcf_prob`, `-bcf_loop`, and/or `-split_num`
