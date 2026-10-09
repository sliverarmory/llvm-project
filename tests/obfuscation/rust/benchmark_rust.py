#!/usr/bin/env python3
"""Bounded ordinary/obfuscated cost comparison for the existing pipeline fixture.

The generated harness includes the unchanged pipeline.rs fixture and calls its
exported probe repeatedly to amortize process startup. This is still a
process-level benchmark, not isolated function throughput. It checks the
fixture's known outputs and changed final machine instructions before writing
size, compiler wall time/RSS, and executable wall time as JSON.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from run_pipeline import expected_output


SOURCE = Path(__file__).with_name("pipeline.rs")
SEED = "00112233445566778899aabbccddeeff"
PASSES = "obf-split,obf-sub"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def peak_rss_bytes(usage):
    if usage is None:
        return None, "os.wait4 is unavailable on this host"
    system = platform.system()
    if system == "Darwin":
        return usage.ru_maxrss, None  # Darwin reports bytes.
    if system == "Linux":
        return usage.ru_maxrss * 1024, None  # Linux reports KiB.
    return None, f"ru_maxrss units are unknown for {system}"


def windows_peak_working_set_reader(process):
    """Read the lifetime peak of an open Windows process handle in bytes."""
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    get_memory = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE,
                           ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
    get_memory.restype = wintypes.BOOL

    def read():
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not get_memory(process._handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize

    return read


def compile_once(command, *, timeout):
    """Wait for one rustc and collect that process's rusage where possible."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        start = time.perf_counter_ns()
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        usage = None
        windows_samples = []
        memory_stop = threading.Event()
        memory_thread = None
        read_windows_memory = None
        if os.name == "nt":
            read_windows_memory = windows_peak_working_set_reader(process)

            def sample_memory():
                while not memory_stop.is_set():
                    try:
                        windows_samples.append(read_windows_memory())
                    except OSError:
                        # The process can exit between the wait and the read.
                        pass
                    memory_stop.wait(0.01)

            memory_thread = threading.Thread(target=sample_memory, daemon=True)
            memory_thread.start()
        if hasattr(os, "wait4"):
            # Popen.wait() would reap the child and lose its individual rusage.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(os.wait4, process.pid, 0)
                try:
                    _, status, usage = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError as error:
                    process.kill()
                    future.result(timeout=10)
                    raise TimeoutError(f"compiler exceeded {timeout}s: {command!r}") from error
            process.returncode = os.waitstatus_to_exitcode(status)
        else:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=10)
                raise TimeoutError(f"compiler exceeded {timeout}s: {command!r}") from error
            finally:
                if memory_thread:
                    memory_stop.set()
                    memory_thread.join()
            if read_windows_memory:
                try:
                    windows_samples.append(read_windows_memory())
                except OSError:
                    pass
        wall_ns = time.perf_counter_ns() - start
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError(
            f"compiler exited {process.returncode}: {command!r}\n"
            f"stdout:\n{output}\nstderr:\n{errors}"
        )
    if read_windows_memory:
        if not windows_samples:
            raise RuntimeError("Windows process peak working set was unavailable")
        rss, reason = max(windows_samples), None
    else:
        rss, reason = peak_rss_bytes(usage)
    return {"wall_ns": wall_ns, "peak_rss_bytes": rss,
            "peak_rss_unavailable_reason": reason}


def run_program(path, *, expected, timeout):
    start = time.perf_counter_ns()
    result = subprocess.run([str(path)], capture_output=True, text=True,
                            timeout=timeout)
    wall_ns = time.perf_counter_ns() - start
    if result.returncode:
        raise RuntimeError(
            f"{path} exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
    if expected is not None and result.stdout != expected:
        raise RuntimeError(f"{path} changed the fixture output: {result.stdout!r}")
    return wall_ns, result.stdout


def machine_mnemonics(objdump, path):
    symbol = "_pipeline_probe" if platform.system() == "Darwin" else "pipeline_probe"
    result = subprocess.run(
        [str(objdump), f"--disassemble-symbols={symbol}", "--no-show-raw-insn",
         str(path)], capture_output=True, text=True, timeout=30, check=True,
    )
    if f"<{symbol}>:" not in result.stdout:
        raise RuntimeError(f"linked function {symbol} absent in {path}")
    instructions = re.findall(
        r"(?m)^\s*[0-9a-f]+:\s+([a-z][a-z0-9_.]*)\b", result.stdout
    )
    if not instructions:
        raise RuntimeError(f"no linked machine instructions found in {path}")
    return instructions


def median(samples):
    return statistics.median(samples)


def checkout_state():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=SOURCE.parents[3],
                          capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                            cwd=SOURCE.parents[3], capture_output=True, text=True,
                            timeout=60, check=True).stdout
    return head, bool(status)


def write_harness(work, iterations):
    expected = [int(value, 16) for value in expected_output().splitlines()]
    if len(expected) != 4:
        raise RuntimeError("pipeline fixture oracle has changed")
    # A module include retains the existing pipeline_probe implementation and
    # unmangled symbol. Its fixture main becomes an unused module function.
    source = f"""mod fixture {{ include!({json.dumps(str(SOURCE))}); }}
fn main() {{
    let seeds = [0_u64, 1, 0x1234_5678_9abc_def0, u64::MAX];
    let expected = [{', '.join(f'{value}_u64' for value in expected)}];
    for (seed, answer) in seeds.into_iter().zip(expected) {{
        assert_eq!(fixture::pipeline_probe(std::hint::black_box(seed)), answer);
    }}
    let mut checksum = 0_u64;
    for seed in 0..{iterations}_u64 {{
        checksum = checksum.wrapping_add(
            fixture::pipeline_probe(std::hint::black_box(seed)) ^ seed.rotate_left(7));
    }}
    println!("{{checksum:016x}}");
}}
"""
    harness = work / "pipeline_benchmark.rs"
    harness.write_text(source)
    return harness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--compile-runs", type=int, default=3,
                        help="paired clean rustc builds per variant (1-5)")
    parser.add_argument("--runtime-runs", type=int, default=11,
                        help="process launches per variant after warmup (3-25)")
    parser.add_argument("--iterations", type=int, default=100_000,
                        help="pipeline_probe calls per process (1000-200000)")
    args = parser.parse_args()
    if (not 1 <= args.compile_runs <= 5 or not 3 <= args.runtime_runs <= 25
            or not 1_000 <= args.iterations <= 200_000):
        parser.error("compile runs must be 1-5, runtime runs 3-25, and iterations 1000-200000")

    rustc = args.rustc.resolve(strict=True)
    objdump = args.objdump.resolve(strict=True)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    harness = write_harness(work, args.iterations)
    version = subprocess.run([str(rustc), "-vV"], capture_output=True, text=True,
                             timeout=30, check=True).stdout.strip()
    if not re.search(r"(?m)^release: 1\.99\.", version) or not re.search(
            r"(?m)^LLVM version: 23\.", version):
        raise RuntimeError(f"expected the custom Rust 1.99 / LLVM 23 toolchain:\n{version}")

    samples = {name: {"compile_wall_ns": [], "compile_peak_rss_bytes": [],
                      "compile_peak_rss_unavailable_reasons": [],
                      "binary_size_bytes": [], "runtime_wall_ns": []}
               for name in ("ordinary", "obfuscated")}
    final_binaries = {}
    common = [str(rustc), "--edition=2024", "-C", "opt-level=2",
              "-C", "panic=abort", "-C", "codegen-units=1", "-C", "lto=off"]
    if os.name == "nt":
        common += ["-C", "link-arg=/EXPORT:pipeline_probe"]
    llvm = (f"-rust-obf-pipeline={PASSES} "
            f"-obf-only-functions=pipeline_probe -obf-test-seed={SEED}")
    for number in range(args.compile_runs):
        order = ("ordinary", "obfuscated") if number % 2 == 0 else (
            "obfuscated", "ordinary")
        for name in order:
            binary = work / f"{name}-{number}{'.exe' if os.name == 'nt' else ''}"
            command = [*common]
            if name == "obfuscated":
                command += ["-C", f"llvm-args={llvm}"]
            command += [str(harness), "-o", str(binary)]
            reading = compile_once(command, timeout=180)
            samples[name]["compile_wall_ns"].append(reading["wall_ns"])
            samples[name]["compile_peak_rss_bytes"].append(
                reading["peak_rss_bytes"])
            samples[name]["compile_peak_rss_unavailable_reasons"].append(
                reading["peak_rss_unavailable_reason"])
            samples[name]["binary_size_bytes"].append(binary.stat().st_size)
            final_binaries[name] = binary
            print(f"compiled {name} pair {number + 1}/{args.compile_runs}", flush=True)

    ordinary_code = machine_mnemonics(objdump, final_binaries["ordinary"])
    protected_code = machine_mnemonics(objdump, final_binaries["obfuscated"])
    if ordinary_code == protected_code:
        raise RuntimeError("selected pipeline_probe has unchanged final instructions")

    _, expected = run_program(final_binaries["ordinary"], expected=None, timeout=10)
    if not re.fullmatch(r"[0-9a-f]{16}\n", expected):
        raise RuntimeError(f"benchmark harness gave malformed checksum: {expected!r}")
    for name in ("ordinary", "obfuscated"):
        for _ in range(2):
            run_program(final_binaries[name], expected=expected, timeout=10)
    for number in range(args.runtime_runs):
        order = ("ordinary", "obfuscated") if number % 2 == 0 else (
            "obfuscated", "ordinary")
        for name in order:
            wall_ns, _ = run_program(final_binaries[name], expected=expected,
                                     timeout=10)
            samples[name]["runtime_wall_ns"].append(wall_ns)

    measurements = {}
    for name, item in samples.items():
        rss_values = [value for value in item["compile_peak_rss_bytes"]
                      if value is not None]
        rss_reasons = sorted({reason for reason in
                              item["compile_peak_rss_unavailable_reasons"] if reason})
        measurements[name] = {
            **item,
            "compile_wall_median_ns": median(item["compile_wall_ns"]),
            "compile_peak_rss_median_bytes": median(rss_values) if rss_values else None,
            "compile_peak_rss_unavailable_reason": (
                None if rss_values else "; ".join(rss_reasons)),
            "binary_size_median_bytes": median(item["binary_size_bytes"]),
            "runtime_wall_median_ns": median(item["runtime_wall_ns"]),
        }
    ordinary = measurements["ordinary"]
    protected = measurements["obfuscated"]
    checkout_head, checkout_dirty = checkout_state()
    report = {
        "schema_version": 1,
        "fixture": str(SOURCE.relative_to(Path(__file__).resolve().parents[3])),
        "fixture_sha256": sha256(SOURCE),
        "generated_harness_sha256": sha256(harness),
        "checkout_head_at_measurement": checkout_head,
        "checkout_dirty_at_measurement": checkout_dirty,
        "platform": platform.platform(),
        "rustc_vv": version,
        "configuration": {"opt_level": 2, "panic": "abort", "codegen_units": 1,
                          "lto": "off", "passes": PASSES.split(","),
                          "function": "pipeline_probe", "fixed_seed": SEED,
                          "compile_runs": args.compile_runs,
                          "runtime_runs": args.runtime_runs, "warmup_runs": 2,
                          "probe_calls_per_runtime_run": args.iterations + 4,
                          "compile_timeout_seconds": 180,
                          "runtime_timeout_seconds": 10},
        "measurements": measurements,
        "ratios_obfuscated_over_ordinary": {
            "compile_wall": (protected["compile_wall_median_ns"] /
                             ordinary["compile_wall_median_ns"]),
            "binary_size": (protected["binary_size_median_bytes"] /
                            ordinary["binary_size_median_bytes"]),
            "runtime_wall": (protected["runtime_wall_median_ns"] /
                             ordinary["runtime_wall_median_ns"]),
            "compile_peak_rss": (
                protected["compile_peak_rss_median_bytes"] /
                ordinary["compile_peak_rss_median_bytes"]
                if ordinary["compile_peak_rss_median_bytes"] and
                protected["compile_peak_rss_median_bytes"] else None),
        },
        "verification": {
            "exact_output_sha256": hashlib.sha256(expected.encode()).hexdigest(),
            "known_fixture_oracle_checked": True,
            "final_pipeline_probe_instructions_differ": True,
            "ordinary_instruction_count": len(ordinary_code),
            "obfuscated_instruction_count": len(protected_code),
            "ordinary_binary_sha256": sha256(final_binaries["ordinary"]),
            "obfuscated_binary_sha256": sha256(final_binaries["obfuscated"]),
        },
        "caveats": [
            "Wall times include process startup and, for compilation, linking; 100000 probe calls by default amortize but do not remove launch overhead.",
            "Peak memory is the top-level rustc process's OS-reported maximum, excluding any separately spawned linker process; Windows uses peak working set and Unix uses ru_maxrss.",
            "Runs share the host with other work, use warm filesystem caches, and are not CPU-pinned; ratios are descriptive, not confidence intervals.",
            "The fixed seed makes this one code shape repeatable; it is not a production entropy recommendation.",
            "Checkout HEAD and tracked-file dirty state describe the measurement environment, not an attestation of which source was linked into rustc.",
        ],
    }
    path = work / "benchmark-report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"PASS ordinary/obfuscated cost comparison: {path}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired, TimeoutError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
