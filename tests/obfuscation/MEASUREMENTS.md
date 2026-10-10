# Measuring cost and checking recovery

Run the per-pass report with this fork's built or extracted tools:

```sh
python3 -B tests/obfuscation/report_size_time.py \
  --clang build-llvm-project/bin/clang \
  --opt build-llvm-project/bin/opt \
  --objdump build-llvm-project/bin/llvm-objdump \
  --work-dir build-llvm-project/per-pass-report
```

The nine variants are the baseline, each of the seven passes independently,
and all seven together. Each uses the same workload and must print an identical
checksum. Mutable scalar accesses exercise global indirection; a comparison
exercises constant encoding; a conditional call preserves a branch for
flattening after O2 optimization. Every measured executable uses O2 and frame
pointers. Windows exports the measured function so its final code can be read
using its PE unwind bounds without including neighboring functions.

`obfuscation-performance.json` records commands, source and binary hashes,
compiler version, raw compile and runtime samples, per-pass compiler events,
skip reasons, and baseline-relative overhead. It also records executable file
size, actual final text/data/BSS section sizes, isolated function instruction
and branch counts, and locations of encoded share-table bytes in the final
file. Executable file size can stay fixed despite substantial code growth due
to alignment; compare text sizes as well.

Final witnesses are separate from compiler events. A reported transformation
is not sufficient evidence that optimization preserved it. An absent final
witness is recorded as false, not converted into a pass. The independent
variants permit attribution; a changed combined artifact alone cannot prove
which pass contributed each instruction. The saved `.asm.txt` files support
inspection of the isolated function. String evidence requires both absence of
the selected plaintext in the file and correct decoded runtime output.

The default report uses two compile samples, three timed runs after one warmup,
and calibration capped at four million iterations. Each subprocess has a
30-second timeout. Compile includes linking and runtime includes process
startup. Compiler memory is the measured process's peak RSS on Linux/macOS or
peak working set on Windows; it is not the sum of simultaneous processes.
These samples describe one machine and fixture, not release performance
thresholds. Increase run counts and runtime calibration for comparisons where
small differences matter. The scripts run natively on the four packaged
platforms; they do not claim cross-target runtime coverage from object files.

Run the separate static-recovery experiment:

```sh
python3 -B tests/obfuscation/report_static_recovery.py \
  --clang build-llvm-project/bin/clang \
  --objdump build-llvm-project/bin/llvm-objdump \
  --work-dir build-llvm-project/static-recovery
```

At O0 and O2, this builds baseline and encoded executables and checks exact
output for six integer inputs, including odd/even indices and unsigned
boundaries. It requires the original selected string and immediate bytes to
be absent from the encoded executable, then attempts recovery:

- The rolling-mask attack knows the string's prefix and allocation length.
  It derives candidate key and step from two bytes and validates the remaining
  prefix. It recovers the unknown suffix directly from executable bytes.
- The constant attack scans read-only sections for adjacent 32-bit rows.
  Two tables with the same row XOR difference are candidates: XORing their
  corresponding rows yields the encoded constant. It records section names
  and final virtual addresses of the matching rows.

Neither recovery algorithm takes LLVM IR, debug data, a compiler seed, or the
expected source constant as input. Expected fixture values are used afterward
to validate the candidates. Compilation uses a fixed test seed for reproducible
fixtures; the recovery functions do not receive that seed. `static-recovery.json` records the recovered data,
locations, runtime outputs, assumptions, and candidate counts. The scanner is
deliberately narrow: it handles these little-endian 32-bit share tables, caps
work on noisy repeated patterns, and does not claim general deobfuscation.

These experiments demonstrate that the encodings obstruct casual inspection
and do not provide secrecy. More sophisticated recovery or resistance metrics
require separate experiments.
