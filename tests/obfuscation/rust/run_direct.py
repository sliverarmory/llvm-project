"""Milestone-0 direct-rustc regression for this fork's seven LLVM passes.

The default mode requires a Rust 1.99 compiler linked to LLVM 23.  A
``--baseline-only`` mode checks the fixtures and local HTTPS test with an
installed rustc, without claiming any obfuscation coverage.
"""

import argparse
import json
import platform
import re
import subprocess
import sys
from pathlib import Path

from machine_code import isolated_instructions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_http_programs import DOCUMENTS, REDIRECTS, start_server  # noqa: E402


ROOT = Path(__file__).resolve().parent
CASES = ((17, 29), (0, 0), (42, 5), (0xFFFFFFFF, 1))
SEED = "00112233445566778899aabbccddeeff"
C_MARKER = "rust-m0-cstring-7e93b1"
PASS_NAMES = (
    "obf-string", "obf-sub", "obf-split", "obf-bcf", "obf-fla",
    "obf-const", "obf-global-access",
)


def run(command, *, timeout=180, check=True):
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"timed out after {timeout}s: {command!r}") from error
    if check and result.returncode:
        raise AssertionError(
            f"failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def compiler_versions(rustc, opt, llvm_config, baseline_only):
    rust = run([str(rustc), "-vV"]).stdout
    llvm = run([str(opt), "--version"]).stdout
    rust_release = re.search(r"^release: (.+)$", rust, re.MULTILINE)
    rust_llvm = re.search(r"^LLVM version: (.+)$", rust, re.MULTILINE)
    opt_llvm = re.search(r"LLVM version (\d+)(?:\.|\s)", llvm)
    require(rust_release and rust_llvm and opt_llvm,
            f"cannot read Rust/LLVM versions:\n{rust}\n{llvm}")
    versions = {"rust_release": rust_release.group(1),
                "rust_llvm": rust_llvm.group(1),
                "opt_llvm": opt_llvm.group(1)}
    if not baseline_only:
        require(rust_release.group(1).startswith("1.99."),
                f"milestone 0 requires Rust 1.99, got {rust_release.group(1)}")
        require(rust_llvm.group(1).split(".")[0] == "23",
                f"custom rustc must use LLVM 23, got {rust_llvm.group(1)}")
        require(opt_llvm.group(1) == "23",
                f"verification opt must use LLVM 23, got {opt_llvm.group(1)}")
    if llvm_config:
        config_version = run([str(llvm_config), "--version"]).stdout.strip()
        versions["llvm_config"] = config_version
        if not baseline_only:
            require(config_version.split(".")[0] == "23",
                    f"llvm-config must use LLVM 23, got {config_version}")
            library = run([str(llvm_config), "--libnames", "Obfuscation"])
            require("LLVMObfuscation" in library.stdout,
                    "llvm-config does not expose LLVMObfuscation")
    return versions


def compile_rust(rustc, source, ir_path, exe_path, *, opt_level,
                 pass_name=None, llvm_options=()):
    command = [
        str(rustc), "--edition=2021", "--crate-name", f"rust_m0_{source.stem}",
        "-C", f"opt-level={opt_level}", "-C", "codegen-units=1",
        "-C", "panic=abort", "-C", "debuginfo=0",
    ]
    if sys.platform == "win32":
        # A linked PE need not retain a COFF symbol table. llvm-objdump reads
        # its export table, which keeps the final-code checks on the executable.
        exported = {"direct": "transform_target",
                    "global_access": "global_target",
                    "https_client": "summarize_checksum"}[source.stem]
        command.extend(("-C", f"link-arg=/EXPORT:{exported}",
                        "-C", "force-frame-pointers=yes"))
    if pass_name:
        command.extend(("-C", f"passes={pass_name}"))
    if llvm_options:
        command.extend(("-C", f"llvm-args={' '.join(llvm_options)}"))
    # rustc can assign different names to private globals in independent
    # `--emit=llvm-ir` and `--emit=link` builds.  Emit both from one invocation
    # so the exact name selected from the baseline IR reaches the linked code.
    command.extend((f"--emit=llvm-ir={ir_path},link={exe_path}", str(source)))
    return run(command)


def compile_pair(rustc, opt, source, stem, *, opt_level, pass_name=None,
                 llvm_options=()):
    ir_path = stem.with_suffix(".ll")
    exe_path = stem.with_suffix(".exe") if sys.platform == "win32" else stem
    result = compile_rust(rustc, source, ir_path, exe_path, opt_level=opt_level,
                          pass_name=pass_name, llvm_options=llvm_options)
    run([str(opt), "-passes=verify", "-disable-output", str(ir_path)])
    return ir_path.read_text(encoding="utf-8"), exe_path, result.stderr


def function_body(ir, name):
    match = re.search(rf"(?m)^define\b[^\n]*@{re.escape(name)}\([^\n]*\)"
                      r"[^\n]*\{\n(?P<body>.*?)^\}", ir, re.DOTALL | re.MULTILINE)
    require(match, f"{name} definition missing from Rust IR")
    return match.group("body")


def function_has_optnone(ir, name):
    signature = re.search(rf"(?m)^define\b[^\n]*@{re.escape(name)}\([^\n]*", ir)
    require(signature, f"{name} signature missing from Rust IR")
    if "optnone" in signature.group(0):
        return True
    attribute = re.search(r"#(\d+)\s*(?:personality|\{)", signature.group(0))
    if not attribute:
        return False
    definition = re.search(
        rf"(?m)^attributes #{attribute.group(1)} = \{{(?P<attrs>[^\n]*)\}}", ir)
    return bool(definition and re.search(r"\boptnone\b", definition.group("attrs")))


def instruction_count(body):
    return len(re.findall(r"(?m)^\s+%[^=\n]+\s=\s", body))


def branch_count(body):
    return len(re.findall(r"(?m)^\s+br\s", body))


def selected_name(ir, marker):
    for line in ir.splitlines():
        if line.startswith("@") and marker in line and " = " in line:
            name = line[1:].split(" = ", 1)[0]
            return name[1:-1] if name.startswith('"') and name.endswith('"') else name
    raise AssertionError(f"global containing {marker!r} missing from baseline IR")


def baseline_witnesses(ir):
    body = function_body(ir, "transform_target")
    require("add i32 %a, 23063" in body,
            "selected i32:0x5a17 arithmetic site missing from optimized Rust IR")
    require(branch_count(body) >= 2 and "switch i32" in body,
            "optimized Rust fixture lacks real branches and switch")
    require(C_MARKER in ir, "NUL-terminated string marker absent from baseline IR")
    marker = selected_name(ir, C_MARKER)
    require(re.search(rf"(?m)^@{re.escape(marker)} = private .* c\"{C_MARKER}\\00\"", ir),
            "string marker must be a private NUL-terminated byte array")
    return {"string": marker}


def global_witness(ir):
    state = selected_name(ir, "STATE")
    require(re.search(rf"(?m)^@{re.escape(state)} = internal .* global i32", ir),
            "STATE must have local scalar linkage for global-access eligibility")
    signature = re.search(r"(?m)^define[^\n]*@global_target\([^\n]*", ir)
    require(signature and "personality ptr" not in signature.group(0),
            "global_target has an EH personality, which this pass currently skips")
    require(state in function_body(ir, "global_target"),
            "global_target does not access its selected scalar")
    return state


def pass_options(name, selected):
    options = [f"-obf-test-seed={SEED}", "-obf-report-skips"]
    if name == "obf-string":
        options.append(f"-sobf-only-globals={selected['string']}")
    elif name == "obf-global-access":
        options.extend((f"-gai-only-globals={selected['counter']}",
                        "-obf-only-functions=global_target"))
    else:
        options.append("-obf-only-functions=transform_target")
    if name == "obf-bcf":
        options.append("-bcf_prob=100")
    elif name == "obf-split":
        options.append("-split_num=2")
    elif name == "obf-const":
        options.append("-constenc-values=i32:0x5a17")
    return options


def effect_error(name, ir, baseline, selected):
    target = "global_target" if name == "obf-global-access" else "transform_target"
    body = function_body(ir, target)
    base = function_body(baseline, target)
    if name == "obf-string":
        if C_MARKER in ir or "@llvm.global_ctors" not in ir or ".datadiv_decode" not in ir:
            return "selected C-string was not encoded with a decoder"
    elif name == "obf-sub":
        if instruction_count(body) <= instruction_count(base):
            return "target arithmetic did not expand"
    elif name == "obf-split":
        if branch_count(body) <= branch_count(base) or ".split" not in body:
            return "target basic blocks were not split"
    elif name == "obf-bcf":
        if ("@.obf.bcf.x" not in ir or "@.obf.bcf.y" not in ir or
                "urem i32" not in body or branch_count(body) <= branch_count(base)):
            return "target bogus-control-flow predicate was not emitted"
    elif name == "obf-fla":
        if ("switchVar" not in body or "switchDefault:" not in body or
                "loopEntry:" not in body or body == base):
            return "target flattening dispatcher was not emitted"
    elif name == "obf-const":
        if (".obf.const.a" not in ir or ".obf.const.b" not in ir or
                "obf.const.decoded" not in body or "add i32 %a, 23063" in body):
            return "selected integer constant was not encoded"
    elif name == "obf-global-access":
        if (f".obf.gai.get.{selected['counter']}" not in ir or
                "obf.gai.ptr" not in body):
            return "selected private scalar access was not indirected"
    else:
        raise AssertionError(f"unknown effect check: {name}")
    return None


def disassembled_function(objdump, executable, symbol):
    raw = "_" + symbol if platform.system() == "Darwin" else symbol
    return [line.split(None, 1)[0]
            for line in isolated_instructions(objdump, executable, raw)]


def runtime_output(executable):
    return [run([str(executable), str(a), str(b)], timeout=10).stdout
            for a, b in CASES]


def expected_direct(a, b):
    mask = 0xFFFFFFFF
    state = 7

    def rotate_left(value, count):
        return ((value << count) | (value >> (32 - count))) & mask

    def rotate_right(value, count):
        return ((value >> count) | (value << (32 - count))) & mask

    def step(x, y):
        nonlocal state
        before = state
        mixed = ((x + 0x5A17) & mask) ^ rotate_left(y, 3)
        if mixed & 1 == 0:
            branch = (mixed * 3 + y) & mask
        else:
            branch = rotate_right((mixed - y) & mask, 1)
        match branch & 3:
            case 0:
                result = branch ^ 0xA5A5
            case 1:
                result = (branch + x) & mask
            case 2:
                result = branch * 5 & mask
            case _:
                result = branch ^ y
        state = (before + (result ^ x)) & mask
        return result ^ before

    first = step(a, b)
    second = step(b, a)
    return f"{first}:{second}:{C_MARKER}\n"


def check_unknown_pass(rustc, work_dir):
    output = work_dir / "unknown-pass.ll"
    output.unlink(missing_ok=True)
    result = run([str(rustc), "--edition=2021", "--crate-name", "unknown_pass_probe",
                  "-C", "passes=obf-no-such-pass", "--emit=llvm-ir",
                  str(ROOT / "direct.rs"), "-o", str(output)], check=False)
    require(result.returncode != 0 and not output.exists(),
            "unknown pass unexpectedly succeeded or emitted IR")
    diagnostic = result.stderr.lower()
    require("pass" in diagnostic and ("unknown" in diagnostic or "not registered" in diagnostic),
            f"unknown pass failed without a useful diagnostic:\n{result.stderr}")


def checksum(body):
    value = 0xCBF29CE484222325
    for index, byte in enumerate(body):
        value ^= byte + (index & 0xFF)
        value = value * 0x100000001B3 & 0xFFFFFFFFFFFFFFFF
        if value & 1 == 0:
            value ^= 0xA5A5A5A5
    return value


def expected_http(document):
    html = document.decode("utf-8")
    title = html.split("<title>", 1)[1].split("</title>", 1)[0]
    return (f"status=200;title={title};links={html.count('<a ')};"
            f"bytes={len(document)};checksum={checksum(document):016x}\n")


def run_local_https(binaries, work_dir):
    server, thread, certificate = start_server(work_dir)
    try:
        for label, executable in binaries.items():
            for start_path, destination in REDIRECTS.items():
                before = len(server.paths)
                url = f"https://127.0.0.1:{server.server_port}{start_path}"
                actual = run([str(executable), url, str(certificate)], timeout=20).stdout
                expected = expected_http(DOCUMENTS[destination][0])
                require(actual == expected,
                        f"{label} HTTPS response mismatch: {actual!r} != {expected!r}")
                require(server.paths[before:] == [start_path, destination],
                        f"{label} did not fetch and follow redirect: {server.paths[before:]!r}")
        baseline = binaries["baseline"]
        bad_cert = run([str(baseline), f"https://127.0.0.1:{server.server_port}/page"],
                       timeout=20, check=False)
        require(bad_cert.returncode != 0,
                "HTTPS fixture accepted its untrusted self-signed certificate")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_https(rustc, opt, objdump, work_dir, baseline_only, live):
    source = ROOT / "https_client.rs"
    baseline, baseline_exe, _ = compile_pair(
        rustc, opt, source, work_dir / "https-O2-baseline", opt_level=2)
    base_body = function_body(baseline, "summarize_checksum")
    require("mul i64" in base_body and "br " in base_body,
            "HTTPS checksum function lacks dynamic arithmetic and control flow")
    binaries = {"baseline": baseline_exe}
    if not baseline_only:
        transformed, transformed_exe, _ = compile_pair(
            rustc, opt, source, work_dir / "https-O2-obf-sub", opt_level=2,
            pass_name="obf-sub", llvm_options=(
                f"-obf-test-seed={SEED}",
                "-obf-only-functions=summarize_checksum",
            ))
        changed = function_body(transformed, "summarize_checksum")
        require(instruction_count(changed) > instruction_count(base_body),
                "HTTPS client's checksum function has no substitution IR effect")
        require(disassembled_function(objdump, baseline_exe, "summarize_checksum") !=
                disassembled_function(objdump, transformed_exe, "summarize_checksum"),
                "HTTPS client's final checksum machine code did not change")
        binaries["obf-sub"] = transformed_exe
    run_local_https(binaries, work_dir)
    print("local certificate-validated HTTPS, redirects, exact HTML checksums: passed", flush=True)
    if live:
        for label, executable in binaries.items():
            response = run([str(executable), "https://example.com/"], timeout=25).stdout
            require(response.startswith("status=200;title=") and ";bytes=" in response,
                    f"{label}: unexpected example.com response: {response!r}")
            print(f"{label} https://example.com/: {response.strip()}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--llvm-config", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--live-https", action="store_true",
                        help="also fetch https://example.com/ with cert validation")
    args = parser.parse_args()
    for label, path in (("rustc", args.rustc), ("opt", args.opt),
                        ("objdump", args.objdump)):
        require(path.is_file(), f"{label} executable missing: {path}")
    if args.llvm_config:
        require(args.llvm_config.is_file(), f"llvm-config missing: {args.llvm_config}")
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    # rustup's rustc is a symlink to the dispatcher; resolving it would run
    # `rustup -vV` instead of `rustc -vV`.
    versions = compiler_versions(args.rustc.absolute(), args.opt.absolute(),
                                 args.llvm_config.absolute() if args.llvm_config else None,
                                 args.baseline_only)
    print(f"versions: {versions}", flush=True)

    source = ROOT / "direct.rs"
    baselines = {}
    global_baselines = {}
    for level in (0, 2):
        ir, executable, _ = compile_pair(
            args.rustc, args.opt, source,
            work_dir / f"direct-O{level}-baseline", opt_level=level)
        output = runtime_output(executable)
        require(output == [expected_direct(a, b) for a, b in CASES],
                "direct Rust baseline differs from independent arithmetic oracle")
        baselines[level] = (ir, executable, output)
        global_ir, global_exe, _ = compile_pair(
            args.rustc, args.opt, ROOT / "global_access.rs",
            work_dir / f"global-O{level}-baseline", opt_level=level)
        global_output = run([str(global_exe)], timeout=10).stdout
        require(global_output == "23:37\n",
                f"private-global Rust baseline returned {global_output!r}, expected '23:37\\n'")
        global_baselines[level] = (global_ir, global_exe, global_output)
    selected_by_level = {
        0: {
            "string": selected_name(baselines[0][0], C_MARKER),
            "counter": selected_name(global_baselines[0][0], "STATE"),
        },
        2: baseline_witnesses(baselines[2][0]),
    }
    selected_by_level[2]["counter"] = global_witness(global_baselines[2][0])
    o0_has_optnone = function_has_optnone(baselines[0][0], "transform_target")
    report = {"versions": versions,
              "selected": {f"O{level}": names
                           for level, names in selected_by_level.items()},
              "o0_has_optnone": o0_has_optnone, "passes": {},
              "complete": False}
    report_path = work_dir / "rust-m0-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"Rust O0 optnone present: {o0_has_optnone}", flush=True)

    if not args.baseline_only:
        check_unknown_pass(args.rustc, work_dir)
        base_machine = {
            level: {
                "transform_target": disassembled_function(
                    args.objdump, baselines[level][1], "transform_target"),
                "global_target": disassembled_function(
                    args.objdump, global_baselines[level][1], "global_target"),
            }
            for level in (0, 2)
        }
        for level in (0, 2):
            require(C_MARKER.encode() in baselines[level][1].read_bytes(),
                    f"O{level} baseline artifact lacks selected plaintext marker")
        for name in PASS_NAMES:
            report["passes"][name] = {}
            for level in (0, 2):
                use_global = name == "obf-global-access"
                fixture = ROOT / "global_access.rs" if use_global else source
                baseline = global_baselines[level] if use_global else baselines[level]
                prefix = "global" if use_global else "direct"
                ir, executable, diagnostics = compile_pair(
                    args.rustc, args.opt, fixture,
                    work_dir / f"{prefix}-O{level}-{name}", opt_level=level,
                    pass_name=name,
                    llvm_options=pass_options(name, selected_by_level[level]))
                selected_names = selected_by_level[level]
                report["passes"][name][f"O{level}_selected_skips"] = [
                    line for line in diagnostics.splitlines()
                    if line.startswith("obf-skip ") and
                    (f'symbol="{selected_names["counter"]}"' in line or
                     f'symbol="{selected_names["string"]}"' in line or
                     'symbol="transform_target"' in line or
                     'symbol="global_target"' in line)
                ]
                output = (run([str(executable)], timeout=10).stdout if use_global
                          else runtime_output(executable))
                require(output == baseline[2],
                        f"{name}/O{level} changed program output")
                error = effect_error(name, ir, baseline[0], selected_names)
                report["passes"][name][f"O{level}_effect"] = error is None
                report_path.write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
                if name == "obf-global-access" and level == 0 and error is not None:
                    # When O0 lowers STATE to a byte array, the scalar pass
                    # must report its precise ineligibility and leave code.
                    require(any(
                        f'symbol="{selected_names["counter"]}" reason=not-integer'
                        in line for line in
                        report["passes"][name]["O0_selected_skips"]),
                        "obf-global-access/O0 did not report the expected scalar skip")
                    require(disassembled_function(args.objdump, executable,
                                                   "global_target") ==
                            base_machine[level]["global_target"],
                            "obf-global-access/O0 changed excluded final code")
                else:
                    require(error is None, f"{name}/O{level}: {error}")
                    if name == "obf-string":
                        require(C_MARKER.encode() not in executable.read_bytes(),
                                f"{name}/O{level}: plaintext remains in final artifact")
                    else:
                        target = ("global_target" if name == "obf-global-access"
                                  else "transform_target")
                        changed_machine = disassembled_function(
                            args.objdump, executable, target)
                        require(changed_machine != base_machine[level][target],
                                f"{name}/O{level}: final {target} machine code unchanged")
                print(f"{name}/O{level}: parses, verifies, runs; effect={error is None}",
                      flush=True)

    test_https(args.rustc, args.opt, args.objdump, work_dir,
               args.baseline_only, args.live_https)
    report["complete"] = True
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Rust milestone-0 direct regression passed", flush=True)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(f"Rust milestone-0 regression failed: {error}", file=sys.stderr)
        sys.exit(1)
