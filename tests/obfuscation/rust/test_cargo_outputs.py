#!/usr/bin/env python3
"""Exercise selected Cargo outputs, Rust/C consumers, and final machine code.

This is deliberately a small native acceptance matrix: one optimized build
contains a selected bin, no_std rlib, Rust dylib, C cdylib, and C staticlib.
Both the ordinary and obfuscated outputs must run with fixed expected values.
The selected build must also change the final code of each exported probe.
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
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
    env["RUSTFLAGS"] = "-C prefer-dynamic"
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


def machine_instructions(objdump, path, symbol):
    raw = "_" + symbol if sys.platform == "darwin" else symbol
    output = run([str(objdump), f"--disassemble-symbols={raw}",
                  "--no-show-raw-insn", str(path)], timeout=60).stdout
    assert f"<{raw}>:" in output, (path, symbol, output[:2000])
    instructions = re.findall(r"(?m)^\s*[0-9a-f]+:\s+([a-z][a-z0-9_.]*)\b", output)
    assert instructions, (path, symbol, output[:2000])
    return instructions


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
                if native_lib.startswith("/LIBPATH:"):
                    command.extend(("-Xlinker", native_lib))
                else:
                    command.append(native_lib)
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
    if cc:
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
        assert any(event["event"] == "effect" and event["raw_name"] == symbol
                   for event in package["events"]), (name, symbol, package["events"])
        assert any(invocation["crate_name"] == name.replace("-", "_")
                   and kind in invocation["crate_type"]
                   and invocation["status"] == "selected"
                   for invocation in report["invocations"]), (name, kind)
    # These are consumed through Rust links, but are not selected output targets.
    for invocation in report["invocations"]:
        if invocation["crate_name"] in ("obf_output_rlib_consumer",
                                         "obf_output_dylib_consumer"):
            assert invocation["status"] == "unselected", invocation
    names = {event["demangled_name"] for event in packages["obf-output-bin"]["events"]
             if event["event"] == "effect"}
    for witness in ("generic_mix", "{closure#", "async_mix::{closure#"):
        assert any(witness in name for name in names), (witness, sorted(names))


def check_final_effects(objdump, host, baseline, selected, cc):
    before_dir = baseline / host / "release"
    after_dir = selected / host / "release"
    result = {}
    for name, (kind, symbol, _) in OUTPUTS.items():
        if kind == "rlib":
            # An rlib is not final code; inspect the Rust consumer executable.
            before = artifact(before_dir, "obf-output-rlib-consumer", "bin")
            after = artifact(after_dir, "obf-output-rlib-consumer", "bin")
        elif kind == "staticlib":
            if not cc:
                continue
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
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--cc", default="clang")
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    rustc = args.rustc.resolve()
    wrapper = args.wrapper.resolve()
    objdump = args.objdump.resolve()
    host, sysroot, version = toolchain(rustc)
    cc = shutil.which(args.cc)
    if not cc and sys.platform != "win32":
        raise AssertionError(f"C compiler unavailable: {args.cc}")
    if not cc:
        print("SKIP C consumers on Windows: clang driver unavailable", flush=True)
    work = args.work_dir.resolve() / ("run-" + uuid.uuid4().hex[:12])
    work.mkdir(parents=True)
    native_libs = native_static_libs(rustc, work)
    baseline = work / "baseline"
    baseline_env = env_for(rustc, sysroot, host, baseline)
    run([args.cargo, *cargo_args(host)], env=baseline_env)
    ordinary = check_runtime(rustc, host, sysroot, baseline, cc, native_libs)

    config_path = work / "obfuscation.json"
    report_path = work / "selected-report.json"
    config_path.write_text(json.dumps(config(rustc, args.cargo), indent=2) + "\n")
    selected_env = env_for(rustc, sysroot, host, work / "selected-unused-target")
    run([str(wrapper), "--config", str(config_path), "--report",
         str(report_path), "--", *cargo_args(host)], env=selected_env)
    report = json.loads(report_path.read_text())
    check_report(report)
    selected = selected_target_dir(report)
    protected = check_runtime(rustc, host, sysroot, selected, cc, native_libs)
    assert protected == ordinary == {key: value for key, value in EXPECTED.items()
                                       if cc or key not in ("cdylib-c-consumer", "staticlib-c-consumer")}
    final = check_final_effects(objdump, host, baseline, selected, cc)
    (work / "acceptance-result.json").write_text(json.dumps({
        "host": host, "rustc": version, "runtime": protected,
        "final_effects": final,
        "skips": [] if cc else ["Windows C consumers require a clang-compatible driver"],
    }, indent=2) + "\n")
    print(f"PASS Cargo output acceptance: {work}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
