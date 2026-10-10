"""Measure all seven passes separately and together, with final-binary evidence."""

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from binary_evidence import (checked, constant_share_witnesses, final_instructions,
                             final_section_sizes, instruction_metrics, native_flags,
                             sibling_tool)
from benchmark_rust import compile_once, sha256

SOURCE = Path(__file__).with_name("benchmark.c").resolve()
COMMON_FLAGS = ("-O2", "-std=c11", "-fno-omit-frame-pointer", "-fno-discard-value-names")
TEST_SEED = "00112233445566778899aabbccddeeff"
PASS_FLAGS = {
    "sobf": ("-sobf", "-sobf-only-globals=label"),
    "split": ("-split", "-split_num=2"),
    "bcf": ("-bcf", "-bcf_prob=100"),
    "fla": ("-fla",),
    "sub": ("-sub", "-sub_loop=1"),
    "constenc": ("-constenc", "-constenc-values=i32:0xe32b09a7,i32:0xe32b09a6"),
    "gai": ("-gai", "-gai-only-globals=benchmark_state"),
}
VARIANTS = ("baseline", *PASS_FLAGS, "combined")


def compile_command(clang, output, variant, sysroot):
    flags = []
    if variant != "baseline":
        flags = ["-obf-only-functions=transform", f"-obf-test-seed={TEST_SEED}"]
        for name in (PASS_FLAGS if variant == "combined" else (variant,)):
            flags.extend(PASS_FLAGS[name])
    return [str(clang), *COMMON_FLAGS, *native_flags(sysroot),
            *(item for flag in flags for item in ("-mllvm", flag)),
            str(SOURCE), "-o", str(output)]


def benchmark_run(executable, iterations, timeout):
    start = time.perf_counter_ns()
    output = checked([str(executable), str(iterations)], timeout)
    elapsed = (time.perf_counter_ns() - start) / 1e9
    if not output.startswith("obfuscation-benchmark-v1:") or not output.endswith("\n"):
        raise RuntimeError(f"unexpected benchmark output: {output!r}")
    return elapsed, output


def compile_measured(command, event_path, timeout):
    keys = ("OBF_EVENT_FILE", "OBF_EVENT_DIR", "RUST_OBF_EVENT_FILE")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["OBF_EVENT_FILE"] = str(event_path)
        event_path.unlink(missing_ok=True)
        return compile_once(command, timeout=timeout)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def final_evidence(name, ir, binary, instructions, baseline_instructions, events):
    metrics = instruction_metrics(instructions)
    base = instruction_metrics(baseline_instructions)
    selected = list(PASS_FLAGS) if name == "combined" else ([] if name == "baseline" else [name])
    shares = constant_share_witnesses(ir, binary)
    effects = {}
    for transform in selected:
        kind, symbol = ("global", "label") if transform == "sobf" else ("function", "transform")
        count = sum(row.get("count", 0) for row in events
                    if row.get("event") == "effect" and row.get("pass") == transform
                    and row.get("kind") == kind and row.get("raw_name") == symbol)
        if transform == "sobf":
            observed = b"obfuscation-benchmark-v1" not in binary
            witness = "selected label absent from final executable; runtime output verified"
        elif transform == "constenc":
            observed = bool(shares) and all(row["final_file_offset"] is not None for row in shares)
            witness = "encoded share-table bytes located in final executable"
        elif transform == "gai":
            observed = metrics["calls"] > base["calls"]
            witness = "additional calls in isolated final function for pointer helpers"
        elif transform == "split":
            observed = metrics["conditional_branches"] > base["conditional_branches"]
            witness = "additional conditional branches in isolated final function"
        else:
            observed = metrics["mnemonics"] != base["mnemonics"]
            witness = "isolated final function instruction sequence differs from baseline"
        if not count:
            raise RuntimeError(f"{name}: selected pass {transform} reported no transformation")
        effects[transform] = {"compiler_effect_count": count,
                              "final_witness_observed": observed, "witness": witness,
                              "attribution": "combined artifact; compare individual variant" if name == "combined" else "individual-pass variant"}
    return {"function": "transform", **metrics, "effects": effects,
            "constant_share_tables": shares,
            "instructions_sha256": hashlib.sha256("\n".join(instructions).encode()).hexdigest()}


def render_markdown(report):
    rows = []
    for name, item in report["variants"].items():
        ratios = item["overhead_vs_baseline"]
        effects = item["final_evidence"]["effects"]
        witnesses = ", ".join(key for key, value in effects.items() if value["final_witness_observed"]) or "none"
        rows.append(f'| {name} | {item["binary_bytes"]:,} | {item["final_sections"]["text_bytes"]:,} | {ratios["text_size_ratio"]:.2f}x | '
                    f'{item["compile_median_seconds"]:.4f} | {ratios["compile_time_ratio"]:.2f}x | '
                    f'{item["runtime_median_seconds"]:.4f} | {ratios["runtime_ratio"]:.2f}x | {witnesses} |')
    return ("# Obfuscation per-pass measurements\n\n"
            f'Compiler: `{report["compiler"]["version"]}`. Host: `{report["host"]}`.\n\n'
            f'Workload: {report["iterations"]:,} iterations; all variants produced `{report["verified_output"].strip()}`.\n\n'
            "| Variant | Binary bytes | Text bytes | Text ratio | Compile s | Compile ratio | Runtime s | Runtime ratio | Final witnesses |\n"
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |\n" + "\n".join(rows) + "\n\n"
            "Compile includes linking; runtime includes process startup. JSON contains raw samples, "
            "compiler process peak RSS, compiler events, final function counts, and share-table file offsets. "
            "Combined witnesses cannot independently attribute instructions to each pass. Missing witnesses "
            "remain false in JSON rather than being counted as success. These one-host results are not "
            "performance thresholds or evidence of secrecy.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path)
    parser.add_argument("--objdump", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--compile-runs", type=int, default=2)
    parser.add_argument("--runtime-runs", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--minimum-runtime", type=float, default=0.05)
    parser.add_argument("--max-iterations", type=int, default=4_000_000)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if args.compile_runs < 1 or args.runtime_runs < 3 or args.warmups < 0 or args.timeout <= 0:
        parser.error("compile-runs >= 1, runtime-runs >= 3, warmups >= 0, timeout > 0 required")
    if args.minimum_runtime <= 0 or not 1 <= args.max_iterations <= 0xFFFFFFFF:
        parser.error("minimum-runtime > 0 and max-iterations in [1, 2^32-1] required")
    clang = args.clang.resolve()
    opt = args.opt.resolve() if args.opt else sibling_tool(clang, "opt")
    objdump = args.objdump.resolve() if args.objdump else sibling_tool(clang, "llvm-objdump")
    sysroot = args.sysroot.resolve() if args.sysroot else None
    if sysroot is None and sys.platform == "darwin":
        sysroot = Path(checked(["xcrun", "--show-sdk-path"]).strip())
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    executables = {name: work_dir / f"benchmark-{name}{suffix}" for name in VARIANTS}
    compile_samples = {name: [] for name in VARIANTS}
    for index in range(args.compile_runs):
        for name in (VARIANTS if index % 2 == 0 else VARIANTS[::-1]):
            compile_samples[name].append(compile_measured(
                compile_command(clang, executables[name], name, sysroot),
                work_dir / f"{name}-{index}.events.jsonl", args.timeout))
    iterations = min(250_000, args.max_iterations)
    while True:
        elapsed, _ = benchmark_run(executables["baseline"], iterations, args.timeout)
        if elapsed >= args.minimum_runtime or iterations == args.max_iterations:
            break
        iterations = min(iterations * 2, args.max_iterations)
    _, expected = benchmark_run(executables["baseline"], iterations, args.timeout)
    runtime_samples = {name: [] for name in VARIANTS}
    for index in range(args.warmups + args.runtime_runs):
        for name in (VARIANTS if index % 2 == 0 else VARIANTS[::-1]):
            elapsed, output = benchmark_run(executables[name], iterations, args.timeout)
            if output != expected:
                raise RuntimeError(f"{name} runtime differs from baseline: {output!r}")
            if index >= args.warmups:
                runtime_samples[name].append(elapsed)
    baseline_code = final_instructions(objdump, executables["baseline"], "transform")
    report = {"schema_version": 2, "generated_utc": datetime.now(timezone.utc).isoformat(),
              "host": platform.platform(), "compiler": {"path": str(clang),
              "version": checked([str(clang), "--version"]).splitlines()[0],
              "sha256": sha256(clang),
              "sysroot": str(sysroot) if sysroot else None},
              "source": {"path": str(SOURCE), "sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest()},
              "iterations": iterations, "calibration_target_seconds": args.minimum_runtime,
              "compile_runs": args.compile_runs, "runtime_runs": args.runtime_runs,
              "warmups": args.warmups, "verified_output": expected,
              "memory_scope": "compiler driver process peak RSS/working set; not aggregate concurrent memory",
              "variants": {}}
    for name in VARIANTS:
        ir_path = work_dir / f"benchmark-{name}.ll"
        checked([*compile_command(clang, ir_path, name, sysroot), "-S", "-emit-llvm"], args.timeout)
        checked([str(opt), "-passes=verify", "-disable-output", str(ir_path)], args.timeout)
        event_path = work_dir / f"{name}-0.events.jsonl"
        events = [json.loads(line) for line in event_path.read_text().splitlines()] if event_path.exists() else []
        instructions = final_instructions(objdump, executables[name], "transform")
        (work_dir / f"benchmark-{name}.asm.txt").write_text("\n".join(instructions) + "\n", encoding="utf-8")
        evidence = final_evidence(name, ir_path.read_text(encoding="utf-8"), executables[name].read_bytes(),
                                  instructions, baseline_code, events)
        samples = compile_samples[name]
        memory = [sample["peak_rss_bytes"] for sample in samples if sample["peak_rss_bytes"] is not None]
        report["variants"][name] = {
            "command": compile_command(clang, executables[name], name, sysroot),
            "binary_bytes": executables[name].stat().st_size,
            "binary_sha256": hashlib.sha256(executables[name].read_bytes()).hexdigest(),
            "final_sections": final_section_sizes(objdump, executables[name]),
            "compile_samples": samples,
            "compile_median_seconds": statistics.median(sample["wall_ns"] for sample in samples) / 1e9,
            "compile_peak_rss_median_bytes": statistics.median(memory) if memory else None,
            "runtime_seconds": runtime_samples[name],
            "runtime_median_seconds": statistics.median(runtime_samples[name]),
            "events": events, "final_evidence": evidence}
    baseline = report["variants"]["baseline"]
    for item in report["variants"].values():
        item["overhead_vs_baseline"] = {
            "binary_size_ratio": item["binary_bytes"] / baseline["binary_bytes"],
            "text_size_ratio": item["final_sections"]["text_bytes"] / baseline["final_sections"]["text_bytes"],
            "compile_time_ratio": item["compile_median_seconds"] / baseline["compile_median_seconds"],
            "runtime_ratio": item["runtime_median_seconds"] / baseline["runtime_median_seconds"]}
    json_path = work_dir / "obfuscation-performance.json"
    markdown_path = work_dir / "obfuscation-performance.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    print(f"JSON: {json_path}\nMarkdown: {markdown_path}")


if __name__ == "__main__":
    main()
