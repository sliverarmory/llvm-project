"""End-to-end Cargo selection and report gate for the pinned Rust toolchain."""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path


FIXTURE = Path(__file__).resolve().parent / "cargo_fixture"
MEMBER = "rust-obf-member"
REGISTRY = "adler2"
EXPECTED = "23109:23109:1.5:292160369\n"
STRING_MARKER = b"m2-member-text-secret-75b13a"
INLINE_MARKER = b"m2-inline-secret-04b1a8"


def run(command, *, env=None, code=0, timeout=240):
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != code:
        raise AssertionError(
            f"expected exit {code}, got {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def saved_ir_env(base=None):
    env = dict(os.environ if base is None else base)
    if "CARGO_ENCODED_RUSTFLAGS" in env:
        prior = env["CARGO_ENCODED_RUSTFLAGS"]
        env["CARGO_ENCODED_RUSTFLAGS"] = (
            prior + ("\x1f" if prior else "") + "-C\x1fsave-temps=yes")
    else:
        env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") +
                            " -C save-temps=yes").strip()
    return env


def verify_emitted_ir(opt, target_dir, host, crates):
    deps = target_dir / host / "release" / "deps"
    bitcode = sorted(deps.glob("*.rcgu.bc"))
    assert bitcode, f"Cargo emitted no saved LLVM IR in {deps}"
    for crate in crates:
        name = crate.replace("-", "_")
        assert any(path.name.startswith((name + "-", name + "."))
                   for path in bitcode), (crate, bitcode)
    for path in bitcode:
        run([str(opt), "-passes=verify", "-disable-output", str(path)],
            timeout=30)


def host_triple(rustc):
    output = run([str(rustc), "-vV"]).stdout
    release = re.search(r"(?m)^release: (.+)$", output)
    llvm = re.search(r"(?m)^LLVM version: (.+)$", output)
    host = re.search(r"(?m)^host: (.+)$", output)
    assert release and release.group(1).startswith("1.99."), output
    assert llvm and llvm.group(1).startswith("23."), output
    assert host, output
    return host.group(1)


def executable(target_dir, host):
    name = "rust-obf-app.exe" if sys.platform == "win32" else "rust-obf-app"
    return target_dir / host / "release" / name


def machine_instructions(objdump, path, symbol):
    raw = "_" + symbol if platform.system() == "Darwin" else symbol
    output = run([str(objdump), f"--disassemble-symbols={raw}",
                  "--no-show-raw-insn", str(path)]).stdout
    assert f"<{raw}>:" in output, (path, symbol)
    instructions = re.findall(r"(?m)^\s*[0-9a-f]+:\s+([a-z][a-z0-9_.]*)\b", output)
    assert instructions, (path, symbol)
    return instructions


def config(rustc, rules, *, strict=True, cargo="cargo"):
    return {
        "version": 1,
        "rustc": str(rustc),
        "cargo": str(cargo),
        "seed": "00112233445566778899aabbccddeeff",
        "strict": strict,
        "packages": rules,
    }


def member_rule(function="member_value"):
    return {"name": MEMBER, "source": "workspace", "targets": ["rust-obf-member"],
            "crate_types": ["rlib"], "passes": ["obf-split"],
            "functions": [function]}


def registry_rule():
    return {"name": REGISTRY, "version": "2.0.1", "source": "registry",
            "crate_types": ["rlib"], "passes": ["obf-bcf"]}


def string_rule(*, global_name=None, functions=None):
    rule = {"name": MEMBER, "source": "workspace", "targets": ["rust-obf-member"],
            "crate_types": ["rlib"], "passes": ["obf-string"],
            "functions": functions or ["member_value"]}
    if global_name:
        rule["globals"] = [global_name]
    return rule


def cargo_args(offline):
    args = ["build", "--release", "--locked", "--manifest-path",
            str(FIXTURE / "Cargo.toml"), "-p", "rust-obf-app"]
    if offline:
        args.append("--offline")
    return args


def selected_packages(report):
    return {item["rule"]["name"]: item for item in report["packages"]}


def assert_selection(report, selected):
    packages = selected_packages(report)
    assert set(packages) == set(selected), packages.keys()
    assert report["code_artifact"] and report["strict_passed"], report
    for name in selected:
        assert packages[name]["unmatched_targets"] == [], (name, packages[name])
        assert packages[name]["unmatched_crate_types"] == [], (name, packages[name])
        pass_name = packages[name]["rule"]["passes"][0]
        summary = packages[name]["pass_summary"][pass_name]
        assert summary["transformed_symbols"] > 0, (name, summary)
        assert summary["transformed_sites"] > 0, (name, summary)
        assert packages[name]["compiled"] > 0, (name, packages[name])
        assert any(event["event"] == "effect" and event["raw_name"]
                   and event["demangled_name"] for event in packages[name]["events"])
    for invocation in report["invocations"]:
        if invocation["crate_name"] in ("rust_obf_app", "itoa", "ryu", "rust_obf_macro_host",
                                         "build_script_build"):
            assert invocation["status"] == "unselected", invocation


def build_with_wrapper(wrapper, rustc, cargo, opt, host, work_dir, rules, label,
                       *, offline=True, strict=True, expected_code=0,
                       extra_env=None):
    config_path = work_dir / f"{label}-config.json"
    report_path = work_dir / f"{label}-report.json"
    config_path.write_text(json.dumps(config(rustc, rules, strict=strict, cargo=cargo), indent=2) + "\n")
    command = [str(wrapper), "--config", str(config_path), "--report",
               str(report_path), "--", *cargo_args(offline)]
    env = saved_ir_env()
    env.update(extra_env or {})
    result = run(command, env=env, code=expected_code, timeout=600)
    assert report_path.exists(), result.stderr
    report = json.loads(report_path.read_text())
    if expected_code == 0:
        verify_emitted_ir(opt, Path(report["target_dir"]), host,
                          (rule["name"] for rule in rules))
    return report, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--online", action="store_true",
                        help="allow Cargo to fetch the pinned registry crates")
    args = parser.parse_args()
    wrapper = args.wrapper.resolve()
    rustc = args.rustc.resolve()
    opt = args.opt.resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    host = host_triple(rustc)
    env = saved_ir_env()
    env["RUSTC"] = str(rustc)
    env["CARGO_TARGET_DIR"] = str(work_dir / "baseline-target")
    baseline = [args.cargo, *cargo_args(not args.online), "--target", host]
    run(baseline, env=env, timeout=600)
    verify_emitted_ir(opt, Path(env["CARGO_TARGET_DIR"]), host,
                      (MEMBER, REGISTRY))
    ordinary = executable(Path(env["CARGO_TARGET_DIR"]), host)
    assert run([str(ordinary)]).stdout == EXPECTED

    # The wrapper owns its fresh target directory, so these are independent
    # builds even when Cargo fingerprints would otherwise reuse dependencies.
    report, _ = build_with_wrapper(wrapper, rustc, args.cargo, opt, host, work_dir,
                                   [member_rule(), registry_rule()], "both",
                                   offline=not args.online)
    assert_selection(report, [MEMBER, REGISTRY])
    first_event_file = next(item["event_file"] for item in report["invocations"]
                            if item["event_file"])
    both_dir = Path(first_event_file).parents[1] / "target"
    assert run([str(executable(both_dir, host))]).stdout == EXPECTED
    assert machine_instructions(args.objdump, ordinary, "member_value") != \
        machine_instructions(args.objdump, executable(both_dir, host), "member_value"), \
        "selected workspace function has unchanged final machine code"

    registry_only, _ = build_with_wrapper(wrapper, rustc, args.cargo, opt, host, work_dir,
                                          [registry_rule()], "registry-only",
                                          offline=not args.online)
    assert_selection(registry_only, [REGISTRY])
    assert all(item["package_id"] is None for item in registry_only["invocations"]
               if item["crate_name"] == "rust_obf_member")
    registry_binary = executable(Path(registry_only["target_dir"]), host)
    assert run([str(registry_binary)]).stdout == EXPECTED
    registry_effects = [event for event in selected_packages(registry_only)[REGISTRY]["events"]
                        if event["event"] == "effect" and event["pass"] == "bcf"
                        and event["kind"] == "function"]
    assert registry_effects, "selected registry package has no function effect"
    if sys.platform == "win32":
        # PE binaries need an explicit export for llvm-objdump to identify a
        # linked Rust function reliably. Discover its exact mangled name from
        # the first selected build, then export it from both fresh binaries.
        symbol = next((event["raw_name"] for event in registry_effects
                       if event["demangled_name"] == "adler2::adler32_slice"),
                      registry_effects[0]["raw_name"])
        exported_env = saved_ir_env()
        exported_env.update({
            "RUSTC": str(rustc),
            "CARGO_TARGET_DIR": str(work_dir / "registry-exported-baseline"),
            "RUST_OBF_TEST_EXPORT_SYMBOL": symbol,
        })
        run(baseline, env=exported_env, timeout=600)
        exported_baseline = executable(Path(exported_env["CARGO_TARGET_DIR"]), host)
        verify_emitted_ir(opt, Path(exported_env["CARGO_TARGET_DIR"]), host,
                          (MEMBER, REGISTRY))
        assert run([str(exported_baseline)]).stdout == EXPECTED
        exported_report, _ = build_with_wrapper(
            wrapper, rustc, args.cargo, opt, host, work_dir,
            [registry_rule()], "registry-exported", offline=not args.online,
            extra_env={"RUST_OBF_TEST_EXPORT_SYMBOL": symbol})
        assert_selection(exported_report, [REGISTRY])
        exported_binary = executable(Path(exported_report["target_dir"]), host)
        assert run([str(exported_binary)]).stdout == EXPECTED
        assert any(event["event"] == "effect" and event["raw_name"] == symbol
                   for event in selected_packages(exported_report)[REGISTRY]["events"])
        assert machine_instructions(args.objdump, exported_baseline, symbol) != \
            machine_instructions(args.objdump, exported_binary, symbol), \
            "selected registry function has unchanged final machine code"
    else:
        assert any(machine_instructions(args.objdump, ordinary, event["raw_name"]) !=
                   machine_instructions(args.objdump, registry_binary, event["raw_name"])
                   for event in registry_effects), \
            "selected registry function has unchanged final machine code"
    assert machine_instructions(args.objdump, ordinary, "member_value") == \
        machine_instructions(args.objdump, registry_binary, "member_value"), \
        "unselected workspace function changed in the registry-only build"

    # Discover the Rust allocation's raw LLVM name through a successful
    # function-scoped build, then require that exact global and function in a
    # second build. A third build proves that one real effect cannot hide an
    # additional, nonexistent function selector in strict mode.
    string_discovery, _ = build_with_wrapper(
        wrapper, rustc, args.cargo, opt, host, work_dir,
        [string_rule()], "string-discovery",
        offline=not args.online)
    assert_selection(string_discovery, [MEMBER])
    string_globals = [event["raw_name"] for event in
                      selected_packages(string_discovery)[MEMBER]["events"]
                      if event["event"] == "effect" and event["pass"] == "sobf"
                      and event["kind"] == "global"
                      and event["raw_name"].endswith("MEMBER_TEXT")]
    assert len(string_globals) == 1, string_globals
    global_name = string_globals[0]
    anonymous_globals = [event["raw_name"] for event in
                         selected_packages(string_discovery)[MEMBER]["events"]
                         if event["event"] == "effect" and event["pass"] == "sobf"
                         and event["kind"] == "global"
                         and event["raw_name"].startswith(".rust.obf.bytes.")]
    assert len(anonymous_globals) == 1, anonymous_globals
    anonymous_name = anonymous_globals[0]
    assert INLINE_MARKER in ordinary.read_bytes()
    for label in ("anonymous-exact-first", "anonymous-exact-repeat"):
        anonymous_report, _ = build_with_wrapper(
            wrapper, rustc, args.cargo, opt, host, work_dir,
            [string_rule(global_name=anonymous_name)], label,
            offline=not args.online)
        assert_selection(anonymous_report, [MEMBER])
        events = selected_packages(anonymous_report)[MEMBER]["events"]
        assert [event["raw_name"] for event in events
                if event["event"] == "effect" and event["pass"] == "sobf"
                and event["kind"] == "global"] == [anonymous_name], events
        anonymous_binary = executable(Path(anonymous_report["target_dir"]), host)
        assert run([str(anonymous_binary)]).stdout == EXPECTED
        assert INLINE_MARKER not in anonymous_binary.read_bytes()
        assert STRING_MARKER in anonymous_binary.read_bytes()
    selected_string, _ = build_with_wrapper(
        wrapper, rustc, args.cargo, opt, host, work_dir,
        [string_rule(global_name=global_name)], "string-exact",
        offline=not args.online)
    assert_selection(selected_string, [MEMBER])
    string_summary = selected_packages(selected_string)[MEMBER]["pass_summary"]["obf-string"]
    assert string_summary["unmatched_functions"] == []
    assert string_summary["unmatched_globals"] == []
    string_binary = executable(Path(selected_string["target_dir"]), host)
    assert run([str(string_binary)]).stdout == EXPECTED
    assert STRING_MARKER in ordinary.read_bytes()
    assert STRING_MARKER not in string_binary.read_bytes()
    string_unmatched, _ = build_with_wrapper(
        wrapper, rustc, args.cargo, opt, host, work_dir,
        [string_rule(global_name=global_name,
                     functions=["member_value", "definitely_absent"])],
        "string-unmatched", offline=not args.online, expected_code=2)
    skipped_summary = selected_packages(string_unmatched)[MEMBER]["pass_summary"]["obf-string"]
    assert skipped_summary["transformed_symbols"] > 0, skipped_summary
    assert skipped_summary["unmatched_globals"] == [], skipped_summary
    assert skipped_summary["unmatched_functions"] == ["definitely_absent"], skipped_summary
    assert not string_unmatched["strict_passed"]

    member_only, _ = build_with_wrapper(wrapper, rustc, args.cargo, opt, host, work_dir,
                                        [member_rule()], "member-only",
                                        offline=not args.online)
    assert_selection(member_only, [MEMBER])
    assert all(item["package_id"] is None for item in member_only["invocations"]
               if item["crate_name"] == "adler2")

    # Each explicitly requested target and crate type needs its own compiled
    # witness. A real rlib effect must not hide an absent target or dylib.
    incomplete_rule = member_rule()
    incomplete_rule["targets"].append("definitely-absent-target")
    incomplete_rule["crate_types"].append("dylib")
    incomplete, _ = build_with_wrapper(
        wrapper, rustc, args.cargo, opt, host, work_dir,
        [incomplete_rule], "incomplete-target-and-type",
        offline=not args.online, expected_code=2)
    incomplete_package = selected_packages(incomplete)[MEMBER]
    assert incomplete["code_artifact"] and not incomplete["strict_passed"], incomplete
    assert incomplete_package["pass_summary"]["obf-split"]["transformed_symbols"] > 0
    assert incomplete_package["unmatched_targets"] == ["definitely-absent-target"]
    assert incomplete_package["unmatched_crate_types"] == ["dylib"]

    # A selector for a pass family absent from the rule must be rejected at
    # configuration time rather than silently counted as strict success.
    for field, selector, diagnostic in (
            ("globals", "definitely_absent_global",
             "globals require obf-string or obf-global-access"),
            ("constants", "i32:0x5a17", "constants require obf-const")):
        invalid_rule = member_rule()
        invalid_rule[field] = [selector]
        invalid_config = work_dir / f"unused-{field}-config.json"
        invalid_report = work_dir / f"unused-{field}-report.json"
        invalid_config.write_text(json.dumps(config(
            rustc, [invalid_rule], cargo=args.cargo)) + "\n")
        result = run([str(wrapper), "--config", str(invalid_config),
                      "--report", str(invalid_report), "--",
                      *cargo_args(not args.online)], code=1)
        assert diagnostic in result.stderr, result.stderr
        assert not invalid_report.exists(), invalid_report

    unmatched, _ = build_with_wrapper(wrapper, rustc, args.cargo, opt, host, work_dir,
                                       [member_rule("definitely_absent")], "unmatched",
                                       offline=not args.online, expected_code=2)
    summary = unmatched["packages"][0]["pass_summary"]["obf-split"]
    assert not unmatched["strict_passed"]
    assert summary["unmatched_functions"] == ["definitely_absent"], summary

    check_config = work_dir / "check-config.json"
    check_report = work_dir / "check-report.json"
    check_config.write_text(json.dumps(config(rustc, [member_rule()], strict=False,
                                              cargo=args.cargo)) + "\n")
    command = [str(wrapper), "--config", str(check_config), "--report",
               str(check_report), "--", "check", "--locked", "--manifest-path",
               str(FIXTURE / "Cargo.toml"), "-p", "rust-obf-app"]
    if not args.online:
        command.append("--offline")
    result = run(command, timeout=600)
    checked = json.loads(check_report.read_text())
    assert not checked["code_artifact"]
    assert checked["coverage_status"] == "no-protected-code-artifact"
    assert "cargo check produced no protected code artifact" in result.stderr
    print("PASS: Cargo workspace and registry selection, host filtering, strict selectors, check report")


if __name__ == "__main__":
    main()
