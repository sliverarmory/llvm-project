#!/usr/bin/env python3
"""Exercise selected Cargo outputs, Rust/C consumers, and final machine code.

This is deliberately a small native acceptance matrix: one optimized build
contains a selected bin, no_std rlib, Rust dylib, C cdylib, and C staticlib.
Both the ordinary and obfuscated outputs must run with fixed expected values.
The selected build must also change the final code of each exported probe.
"""

import argparse
from contextlib import nullcontext
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


FIXTURE = Path(__file__).with_name("cargo_outputs_fixture")
EXPECTED = {
    "obf-output-bin": "23079:70:23:26\n",
    "obf-output-rlib-consumer": "23088\n",
    "obf-output-dylib-consumer": "23079\n",
    "cdylib-c-consumer": "23079\n",
    "staticlib-c-consumer": "23079\n",
}
OUTPUTS = {
    "obf-output-bin": ("bin", "accept_bin_probe", "obf-sub"),
    "obf-output-rlib": ("rlib", "accept_rlib_probe", "obf-split"),
    "obf-output-dylib": ("dylib", "accept_dylib_probe", "obf-split"),
    "obf-output-cdylib": ("cdylib", "accept_cdylib_probe", "obf-split"),
    "obf-output-staticlib": ("staticlib", "accept_staticlib_probe", "obf-split"),
}
BIN_RUST_WITNESSES = {
    "generic-u32": "obf_output_bin::generic_mix::<u32>",
    "generic-u64": "obf_output_bin::generic_mix::<u64>",
    "closure": "obf_output_bin::main::{closure#0}",
    "async": "obf_output_bin::async_mix::{closure#0}",
}


def run(command, *, env=None, timeout=600):
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"exit {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def toolchain(rustc):
    version = run([str(rustc), "-vV"], timeout=30).stdout
    release = re.search(r"(?m)^release: (.+)$", version)
    llvm = re.search(r"(?m)^LLVM version: (.+)$", version)
    host = re.search(r"(?m)^host: (.+)$", version)
    assert release and release.group(1).startswith("1.99."), version
    assert llvm and llvm.group(1).startswith("23."), version
    assert host, version
    sysroot = Path(run([str(rustc), "--print", "sysroot"], timeout=30).stdout.strip())
    assert sysroot.is_dir(), sysroot
    return host.group(1), sysroot, version


def env_for(rustc, sysroot, host, target_dir):
    env = os.environ.copy()
    env.pop("RUSTC_WRAPPER", None)
    env.pop("RUSTC_WORKSPACE_WRAPPER", None)
    env["RUSTC"] = str(rustc)
    # A Rust dylib consumed by a Rust executable must share the dynamically
    # linked Rust sysroot; the default static std fails with duplicate crates.
    # Save the exact Cargo invocation's LLVM IR for the independent opt gate.
    env["RUSTFLAGS"] = "-C prefer-dynamic -C save-temps=yes"
    env.pop("CARGO_ENCODED_RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    paths = [sysroot / "lib" / "rustlib" / host / "lib",
             sysroot / "bin", sysroot / "lib",
             target_dir / host / "release" / "deps",
             target_dir / host / "release"]
    if sys.platform == "darwin":
        key, sep = "DYLD_LIBRARY_PATH", ":"
    elif sys.platform == "win32":
        key, sep = "PATH", os.pathsep
    else:
        key, sep = "LD_LIBRARY_PATH", ":"
    env[key] = sep.join(map(str, paths)) + sep + env.get(key, "")
    return env


def cargo_args(host):
    return ["build", "--release", "--locked", "--offline", "--workspace",
            "--manifest-path", str(FIXTURE / "Cargo.toml"), "--target", host]


def artifact(directory, stem, kind):
    if kind == "bin":
        suffix = ".exe" if sys.platform == "win32" else ""
        return directory / (stem + suffix)
    name = stem.replace("-", "_")
    if sys.platform == "win32":
        return directory / (name + (".lib" if kind == "staticlib" else ".dll"))
    if kind == "staticlib":
        suffix = ".a"
    elif sys.platform == "darwin":
        suffix = ".dylib"
    else:
        suffix = ".so"
    return directory / ("lib" + name + suffix)


def machine_instructions(objdump, path, symbol, *, required=True):
    # LLVM's raw name is used on ELF/COFF; Mach-O prepends an underscore.
    # Probe both spellings so private Rust symbols and exported C probes use
    # the same final-artifact check on every host.
    candidates = ("_" + symbol, symbol) if sys.platform == "darwin" else (
        symbol, "_" + symbol)
    for raw in candidates:
        output = run([str(objdump), f"--disassemble-symbols={raw}",
                      "--no-show-raw-insn", str(path)], timeout=60).stdout
        if f"<{raw}>:" not in output:
            continue
        instructions = re.findall(
            r"(?m)^\s*[0-9a-f]+:\s+([a-z][a-z0-9_.]*)\b", output)
        assert instructions, (path, symbol, output[:2000])
        return instructions
    if not required:
        return None
    raise AssertionError(f"machine-code symbol missing: {path} {symbol}")


def linked_instructions(objdump, path):
    output = run([str(objdump), "--disassemble", "--no-show-raw-insn",
                  str(path)], timeout=60).stdout
    instructions = re.findall(
        r"(?m)^\s*[0-9a-f]+:\s+([a-z][a-z0-9_.]*)\b", output)
    assert instructions, (path, output[:2000])
    return instructions


def contains_instructions(haystack, needle):
    return any(haystack[index:index + len(needle)] == needle
               for index in range(len(haystack) - len(needle) + 1))


def saved_bin_objects(target_dir, host):
    deps = target_dir / host / "release" / "deps"
    objects = sorted((*deps.glob("obf_output_bin-*.rcgu.o"),
                      *deps.glob("obf_output_bin-*.rcgu.obj")))
    assert objects, deps
    return objects


def saved_object_instructions(objdump, objects, symbol):
    matches = [(path, machine_instructions(objdump, path, symbol, required=False))
               for path in objects]
    matches = [(path, instructions) for path, instructions in matches
               if instructions is not None]
    assert len(matches) == 1, (symbol, matches, objects)
    instructions = matches[0][1]
    # COFF symbol ranges can include alignment bytes after the last return.
    # Linkers may use different padding, so compare the function body itself.
    while instructions and instructions[-1] in ("nop", "nopw", "nopl", "nopq", "int3"):
        instructions = instructions[:-1]
    assert instructions, (symbol, matches)
    return instructions


def verify_emitted_ir(opt, target_dir, host):
    deps = target_dir / host / "release" / "deps"
    bitcode = sorted(deps.glob("*.rcgu.bc"))
    assert bitcode, f"Cargo emitted no saved LLVM IR in {deps}"
    for name in OUTPUTS:
        crate = name.replace("-", "_")
        assert any(path.name.startswith((crate + "-", crate + "."))
                   for path in bitcode), (
            name, bitcode)
    for path in bitcode:
        run([str(opt), "-passes=verify", "-disable-output", str(path)],
            timeout=30)


def compile_c(cc, source, output, *, library=None, native_libs=()):
    command = [cc]
    if sys.platform == "darwin":
        command.extend(["-isysroot", run(["xcrun", "--show-sdk-path"], timeout=30).stdout.strip()])
    command.append(str(source))
    if library:
        command.append(str(library))
        if sys.platform == "win32":
            command.extend(("-Xlinker", "/EXPORT:accept_staticlib_probe"))
            for native_lib in native_libs:
                command.extend(("-Xlinker", native_lib))
    if sys.platform == "linux":
        command.extend(["-ldl", "-lpthread", "-lm", "-lrt", "-lutil"])
    command.extend(["-o", str(output)])
    run(command, timeout=180)
    assert output.is_file(), output


def native_static_libs(rustc, work):
    if sys.platform != "win32":
        return []
    library = work / "native-static-libs-probe.lib"
    result = run([str(rustc), "--edition=2024", "--crate-type=staticlib",
                  "-C", "prefer-dynamic",
                  "--crate-name=obf_output_native_libs_probe",
                  "--print=native-static-libs",
                  str(FIXTURE / "staticlib/src/lib.rs"), "-o", str(library)],
                 timeout=360)
    match = re.search(r"(?m)^note: native-static-libs: (.*)$", result.stderr)
    assert match, f"rustc did not report staticlib native dependencies:\n{result.stderr}"
    return match.group(1).split()


def check_runtime(root, host, sysroot, target_dir, cc, native_libs):
    directory = target_dir / host / "release"
    env = env_for(root, sysroot, host, target_dir)
    observed = {}
    for name in ("obf-output-bin", "obf-output-rlib-consumer",
                 "obf-output-dylib-consumer"):
        path = artifact(directory, name, "bin")
        assert path.is_file(), path
        observed[name] = run([str(path)], env=env, timeout=30).stdout
    cdylib = artifact(directory, "obf-output-cdylib", "cdylib")
    staticlib = artifact(directory, "obf-output-staticlib", "staticlib")
    assert cdylib.is_file() and staticlib.is_file(), (cdylib, staticlib)
    extension = ".exe" if sys.platform == "win32" else ""
    dynamic_exe = target_dir / ("cdylib-c-consumer" + extension)
    static_exe = target_dir / ("staticlib-c-consumer" + extension)
    compile_c(cc, FIXTURE / "cdylib_consumer.c", dynamic_exe)
    compile_c(cc, FIXTURE / "staticlib_consumer.c", static_exe,
              library=staticlib, native_libs=native_libs)
    observed["cdylib-c-consumer"] = run(
        [str(dynamic_exe), str(cdylib)], env=env, timeout=30).stdout
    observed["staticlib-c-consumer"] = run(
        [str(static_exe)], env=env, timeout=30).stdout
    assert set(observed) == set(EXPECTED), (observed, EXPECTED)
    for name, output in observed.items():
        assert output == EXPECTED[name], (name, output, EXPECTED[name])
    return observed


def config(rustc, cargo):
    rules = []
    for name, (kind, symbol, pass_name) in OUTPUTS.items():
        rule = {"name": name, "source": "workspace", "targets": [name],
                "crate_types": [kind], "passes": [pass_name]}
        if name != "obf-output-bin":
            rule["functions"] = [symbol]
        rules.append(rule)
    return {"version": 1, "rustc": str(rustc), "cargo": str(cargo),
            "seed": "00112233445566778899aabbccddeeff",
            "strict": True, "packages": rules}


def check_report(report):
    assert report["code_artifact"] and report["strict_passed"], report
    packages = {item["rule"]["name"]: item for item in report["packages"]}
    assert set(packages) == set(OUTPUTS), packages.keys()
    for name, (kind, symbol, pass_name) in OUTPUTS.items():
        package = packages[name]
        assert package["compiled"] >= 1, (name, package)
        summary = package["pass_summary"][pass_name]
        assert summary["transformed_symbols"] > 0 and summary["transformed_sites"] > 0, (name, summary)
        short_pass = "sub" if pass_name == "obf-sub" else "split"
        effects = [event for event in package["events"]
                   if event["event"] == "effect" and event["pass"] == short_pass]
        names = [event["raw_name"] for event in effects]
        assert names.count(symbol) == 1 and len(names) == len(set(names)), (
            name, symbol, effects)
        assert len(effects) == summary["transformed_symbols"], (name, summary, effects)
        assert sum(event["count"] for event in effects) == summary["transformed_sites"], (
            name, summary, effects)
        assert any(invocation["crate_name"] == name.replace("-", "_")
                   and kind in invocation["crate_type"]
                   and invocation["status"] == "selected"
                   for invocation in report["invocations"]), (name, kind)
    # These are consumed through Rust links, but are not selected output targets.
    for invocation in report["invocations"]:
        if invocation["crate_name"] in ("obf_output_rlib_consumer",
                                         "obf_output_dylib_consumer"):
            assert invocation["status"] == "unselected", invocation
    effects = [event for event in packages["obf-output-bin"]["events"]
               if event["event"] == "effect" and event["pass"] == "sub"]
    symbols = {}
    for witness, demangled in BIN_RUST_WITNESSES.items():
        matches = [event for event in effects
                   if event["demangled_name"] == demangled]
        assert len(matches) == 1, (witness, demangled, effects)
        symbols[witness] = matches[0]["raw_name"]
    return symbols


def check_final_effects(objdump, host, baseline, selected, bin_symbols):
    before_dir = baseline / host / "release"
    after_dir = selected / host / "release"
    result = {}
    for name, (kind, symbol, _) in OUTPUTS.items():
        if kind == "rlib":
            # An rlib is not final code; inspect the Rust consumer executable.
            before = artifact(before_dir, "obf-output-rlib-consumer", "bin")
            after = artifact(after_dir, "obf-output-rlib-consumer", "bin")
        elif kind == "staticlib":
            # The archive is intermediate until the C consumer links it.
            suffix = ".exe" if sys.platform == "win32" else ""
            before = baseline / ("staticlib-c-consumer" + suffix)
            after = selected / ("staticlib-c-consumer" + suffix)
        else:
            before = artifact(before_dir, name, kind)
            after = artifact(after_dir, name, kind)
        original = machine_instructions(objdump, before, symbol)
        protected = machine_instructions(objdump, after, symbol)
        assert original != protected, (name, symbol, original, protected)
        result[name] = {"baseline_instructions": len(original),
                        "selected_instructions": len(protected)}
        print(f"PASS {kind}: {symbol} runs and changes in final machine code", flush=True)
    before = artifact(before_dir, "obf-output-bin", "bin")
    after = artifact(after_dir, "obf-output-bin", "bin")
    rust_witnesses = {}
    if sys.platform == "win32":
        # PE links may discard private Rust names. Compare each named function
        # in rustc's saved COFF objects, then require the selected instruction
        # sequence to survive in the linked executable's code. The sequence
        # must be absent from the ordinary executable.
        before_objects = saved_bin_objects(baseline, host)
        after_objects = saved_bin_objects(selected, host)
        before_linked = linked_instructions(objdump, before)
        after_linked = linked_instructions(objdump, after)
        assert before_linked != after_linked, "linked PE code is unchanged"
        evidence = "saved COFF object and linked PE code"
    else:
        before_linked = after_linked = None
        evidence = "final linked symbol"
    for witness, symbol in bin_symbols.items():
        if before_linked is None:
            original = machine_instructions(objdump, before, symbol)
            protected = machine_instructions(objdump, after, symbol)
        else:
            original = saved_object_instructions(objdump, before_objects, symbol)
            protected = saved_object_instructions(objdump, after_objects, symbol)
        assert original != protected, (witness, symbol, original, protected)
        if before_linked is not None:
            assert contains_instructions(before_linked, original), (
                witness, "baseline object code absent from linked PE")
            assert contains_instructions(after_linked, protected), (
                witness, "selected object code absent from linked PE")
            assert not contains_instructions(before_linked, protected), (
                witness, "selected code already present in baseline PE")
        rust_witnesses[witness] = {
            "baseline_instructions": len(original),
            "selected_instructions": len(protected),
        }
        print(f"PASS bin {witness}: obf-sub effect in {evidence}", flush=True)
    result["obf-output-bin"]["rust_witnesses"] = rust_witnesses
    result["obf-output-bin"]["rust_witness_evidence"] = evidence
    return result


def selected_target_dir(report):
    if "target_dir" in report:
        return Path(report["target_dir"])
    # Reports produced by the original Cargo wrapper predate target_dir.
    first_event = next(item["event_file"] for item in report["invocations"]
                       if item["event_file"])
    return Path(first_event).parents[1] / "target"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--cc", default="clang")
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    rustc = args.rustc.resolve()
    wrapper = args.wrapper.resolve()
    objdump = args.objdump.resolve()
    opt = args.opt.resolve()
    host, sysroot, version = toolchain(rustc)
    cc = shutil.which(args.cc)
    if not cc:
        raise AssertionError(f"C compiler unavailable: {args.cc}")
    work = args.work_dir.resolve() / ("run-" + uuid.uuid4().hex[:12])
    work.mkdir(parents=True)
    native_libs = native_static_libs(rustc, work)
    baseline = work / "baseline"
    baseline_env = env_for(rustc, sysroot, host, baseline)
    run([args.cargo, *cargo_args(host)], env=baseline_env)
    verify_emitted_ir(opt, baseline, host)
    ordinary = check_runtime(rustc, host, sysroot, baseline, cc, native_libs)

    config_path = work / "obfuscation.json"
    config_path.write_text(json.dumps(config(rustc, args.cargo), indent=2) + "\n")
    # The wrapper nests its Cargo target beneath the report path. On Windows,
    # MSVC's linker cannot create the resulting files under a deep --work-dir.
    # Keep the acceptance result there, but use a short, cleaned-up scratch
    # directory for the selected build (RUNNER_TEMP is short on Actions).
    selected_context = (
        tempfile.TemporaryDirectory(prefix="roc-", dir=os.environ.get("RUNNER_TEMP"))
        if sys.platform == "win32" else nullcontext(work)
    )
    with selected_context as selected_root:
        selected_root = Path(selected_root)
        report_path = selected_root / "selected-report.json"
        selected_env = env_for(rustc, sysroot, host,
                               selected_root / "selected-unused-target")
        try:
            run([str(wrapper), "--config", str(config_path), "--report",
                 str(report_path), "--", *cargo_args(host)], env=selected_env)
            report = json.loads(report_path.read_text())
            bin_symbols = check_report(report)
            selected = selected_target_dir(report)
            verify_emitted_ir(opt, selected, host)
            protected = check_runtime(rustc, host, sysroot, selected, cc, native_libs)
            assert protected == ordinary == EXPECTED
            final = check_final_effects(objdump, host, baseline, selected, bin_symbols)
        finally:
            if selected_root != work and report_path.is_file():
                shutil.copy2(report_path, work / "selected-report.json")
    (work / "acceptance-result.json").write_text(json.dumps({
        "host": host, "rustc": version, "runtime": protected,
        "final_effects": final,
        "skips": [],
    }, indent=2) + "\n")
    print(f"PASS Cargo output acceptance: {work}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
