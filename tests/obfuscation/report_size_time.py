"""Compare final binary size, compile time, and runtime of obfuscation passes.

Requires this fork's built Clang. Uses only the Python standard library and
checks that baseline and obfuscated executables print the same checksum.
"""

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


SOURCE = Path(__file__).with_name("benchmark.c").resolve()
COMMON_FLAGS = ("-O2", "-std=c11")
TEST_SEED = "00112233445566778899aabbccddeeff"
OBFUSCATION_FLAGS = (
    "-sobf",
    "-obf-only-functions=transform",
    f"-obf-test-seed={TEST_SEED}",
    "-split",
    "-split_num=2",
    "-bcf",
    "-bcf_prob=30",
    "-fla",
    "-sub",
    "-sub_loop=1",
)


def checked_run(command, timeout):
    start = time.perf_counter_ns()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"timed out after {timeout}s: {command!r}") from error
    elapsed = (time.perf_counter_ns() - start) / 1_000_000_000
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return elapsed, result.stdout


def compile_command(clang, output, variant, sysroot):
    command = [str(clang), *COMMON_FLAGS]
    if sysroot:
        command.extend(("-isysroot", str(sysroot)))
    if variant == "combined":
        command.extend(item for flag in OBFUSCATION_FLAGS for item in ("-mllvm", flag))
    return [*command, str(SOURCE), "-o", str(output)]


def benchmark_run(executable, iterations, timeout):
    elapsed, output = checked_run([str(executable), str(iterations)], timeout)
    if not output.startswith("obfuscation-benchmark-v1:") or not output.endswith("\n"):
        raise RuntimeError(f"unexpected benchmark output from {executable}: {output!r}")
    return elapsed, output


def median(samples):
    return statistics.median(samples)


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def render_markdown(report):
    base = report["variants"]["baseline"]
    combined = report["variants"]["combined"]
    size_ratio = ratio(combined["binary_bytes"], base["binary_bytes"])
    compile_ratio = ratio(combined["compile_median_seconds"], base["compile_median_seconds"])
    runtime_ratio = ratio(combined["runtime_median_seconds"], base["runtime_median_seconds"])
    rows = (
        ("Final binary size", f'{base["binary_bytes"]:,} B',
         f'{combined["binary_bytes"]:,} B', size_ratio),
        ("Compile and link median", f'{base["compile_median_seconds"]:.4f} s',
         f'{combined["compile_median_seconds"]:.4f} s', compile_ratio),
        ("Runtime median", f'{base["runtime_median_seconds"]:.4f} s',
         f'{combined["runtime_median_seconds"]:.4f} s', runtime_ratio),
    )
    table = "\n".join(
        f"| {name} | {baseline} | {obfuscated} | {factor:.2f}× |"
        for name, baseline, obfuscated, factor in rows
    )
    return (
        "# Obfuscation size and time report\n\n"
        f'Generated: {report["generated_utc"]}\n\n'
        f'Compiler: `{report["compiler"]["version"]}`\n\n'
        f'Host: `{report["host"]}`\n\n'
        f'Workload: {report["iterations"]:,} iterations; identical output '
        f'`{report["verified_output"].strip()}`.\n\n'
        "| Metric | Baseline | Combined | Combined / baseline |\n"
        "| --- | ---: | ---: | ---: |\n"
        f"{table}\n\n"
        f'Combined flags: `{" ".join(OBFUSCATION_FLAGS)}`. Both variants use '
        "`-O2 -std=c11`. Compile time includes linking; final binary size is "
        "the executable file size. Runtime is wall time including process "
        f'startup; each median uses {report["runtime_runs"]} runs after '
        f'{report["warmups"]} warmup(s). Compile median uses '
        f'{report["compile_runs"]} builds.\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--compile-runs", type=int, default=3)
    parser.add_argument("--runtime-runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--minimum-runtime", type=float, default=0.10)
    parser.add_argument("--max-iterations", type=int, default=32_000_000)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if args.compile_runs < 1 or args.runtime_runs < 3 or args.warmups < 0:
        parser.error("compile-runs must be positive; runtime-runs >= 3; warmups >= 0")
    if args.minimum_runtime <= 0 or not 1 <= args.max_iterations <= 0xFFFFFFFF:
        parser.error("minimum-runtime must be positive and max-iterations in [1, 2^32-1]")

    clang = args.clang.resolve()
    sysroot = args.sysroot.resolve() if args.sysroot else None
    if sysroot is None and sys.platform == "darwin":
        _, sdk_path = checked_run(["xcrun", "--show-sdk-path"], args.timeout)
        sysroot = Path(sdk_path.strip()).resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    executables = {name: work_dir / f"benchmark-{name}{suffix}"
                   for name in ("baseline", "combined")}
    compile_samples = {name: [] for name in executables}
    for index in range(args.compile_runs):
        for name in (("baseline", "combined") if index % 2 == 0
                     else ("combined", "baseline")):
            elapsed, _ = checked_run(
                compile_command(clang, executables[name], name, sysroot), args.timeout
            )
            compile_samples[name].append(elapsed)

    iterations = min(250_000, args.max_iterations)
    while True:
        calibration = [benchmark_run(executables["baseline"], iterations,
                                     args.timeout)[0] for _ in range(3)]
        if median(calibration) >= args.minimum_runtime or iterations == args.max_iterations:
            break
        iterations = min(iterations * 2, args.max_iterations)

    for _ in range(args.warmups):
        _, baseline_output = benchmark_run(
            executables["baseline"], iterations, args.timeout
        )
        _, combined_output = benchmark_run(
            executables["combined"], iterations, args.timeout
        )
        if combined_output != baseline_output:
            raise RuntimeError("baseline and combined outputs differ during warmup")

    runtime_samples = {name: [] for name in executables}
    verified_output = None
    for index in range(args.runtime_runs):
        outputs = {}
        for name in (("baseline", "combined") if index % 2 == 0
                     else ("combined", "baseline")):
            elapsed, output = benchmark_run(executables[name], iterations, args.timeout)
            runtime_samples[name].append(elapsed)
            outputs[name] = output
        if outputs["baseline"] != outputs["combined"]:
            raise RuntimeError(f"baseline and combined outputs differ on run {index + 1}")
        verified_output = outputs["baseline"]

    _, version_output = checked_run([str(clang), "--version"], args.timeout)
    report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "host": platform.platform(),
        "compiler": {"path": str(clang), "version": version_output.splitlines()[0],
                     "sysroot": str(sysroot) if sysroot else None},
        "source": {"path": str(SOURCE),
                   "sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest()},
        "iterations": iterations,
        "calibration_target_seconds": args.minimum_runtime,
        "compile_runs": args.compile_runs,
        "runtime_runs": args.runtime_runs,
        "warmups": args.warmups,
        "verified_output": verified_output,
        "variants": {},
    }
    for name in executables:
        report["variants"][name] = {
            "command": compile_command(clang, executables[name], name, sysroot),
            "binary_bytes": executables[name].stat().st_size,
            "compile_seconds": compile_samples[name],
            "compile_median_seconds": median(compile_samples[name]),
            "runtime_seconds": runtime_samples[name],
            "runtime_median_seconds": median(runtime_samples[name]),
        }

    json_path = work_dir / "obfuscation-performance.json"
    markdown_path = work_dir / "obfuscation-performance.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    markdown = render_markdown(report)
    markdown_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"JSON: {json_path}\nMarkdown: {markdown_path}")


if __name__ == "__main__":
    main()
