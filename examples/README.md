# LLVM Obfuscation Examples

This directory contains small examples in multiple languages that compile with
the local obfuscating Clang toolchain and validate both behavior and
obfuscation.

## Layout

- `c/`: C example
- `cpp/`: C++ example
- `objc/`: Objective-C example (macOS/Foundation)

Each subdirectory has its own `Makefile` with these common targets:

- `make all`: build baseline and obfuscated binaries
- `make check`: build, run, compare outputs, and verify obfuscation
- `make clean`: remove generated files

## Build Everything

From this directory:

```bash
make all
```

## Verify Everything

```bash
make check
```

`check` validates:

- baseline and obfuscated program outputs are identical
- for C/C++, configured plaintext marker string is not present in obfuscated binaries
- obfuscated LLVM IR contains evidence of control-flow obfuscation growth

The Objective-C sample intentionally excludes `-mllvm -sobf` because string
obfuscation against Foundation-based code is unstable in this branch. It still
demonstrates and validates control-flow/instruction obfuscation.

## Notes

- Toolchain defaults to:
  - `/Users/moloch/git/llvm-obfuscator/build-llvm-project/bin/clang`
  - `/Users/moloch/git/llvm-obfuscator/build-llvm-project/bin/clang++`
- On macOS, SDK sysroot is auto-detected with `xcrun --show-sdk-path`.
