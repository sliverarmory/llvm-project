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


def run(command, *, env=None, code=0, timeout=240):
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != code:
        raise AssertionError(
            f"expected exit {code}, got {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


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
            "crate_types": ["rlib"], "passes": ["obf-sub"]}


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


def build_with_wrapper(wrapper, rustc, cargo, work_dir, rules, label, *, offline=True,
                       strict=True, expected_code=0):
    config_path = work_dir / f"{label}-config.json"
    report_path = work_dir / f"{label}-report.json"
    config_path.write_text(json.dumps(config(rustc, rules, strict=strict, cargo=cargo), indent=2) + "\n")
    command = [str(wrapper), "--config", str(config_path), "--report",
               str(report_path), "--", *cargo_args(offline)]
    result = run(command, code=expected_code, timeout=600)
    assert report_path.exists(), result.stderr
    return json.loads(report_path.read_text()), result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--online", action="store_true",
                        help="allow Cargo to fetch the pinned registry crates")
    args = parser.parse_args()
    wrapper = args.wrapper.resolve()
    rustc = args.rustc.resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    host = host_triple(rustc)
    env = os.environ.copy()
    env["RUSTC"] = str(rustc)
    env["CARGO_TARGET_DIR"] = str(work_dir / "baseline-target")
    baseline = [args.cargo, *cargo_args(not args.online), "--target", host]
    run(baseline, env=env, timeout=600)
    ordinary = executable(Path(env["CARGO_TARGET_DIR"]), host)
    assert run([str(ordinary)]).stdout == EXPECTED

    # The wrapper owns its fresh target directory, so these are independent
    # builds even when Cargo fingerprints would otherwise reuse dependencies.
    report, _ = build_with_wrapper(wrapper, rustc, args.cargo, work_dir,
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

    registry_only, _ = build_with_wrapper(wrapper, rustc, args.cargo, work_dir,
                                          [registry_rule()], "registry-only",
                                          offline=not args.online)
    assert_selection(registry_only, [REGISTRY])
    assert all(item["package_id"] is None for item in registry_only["invocations"]
               if item["crate_name"] == "rust_obf_member")

    member_only, _ = build_with_wrapper(wrapper, rustc, args.cargo, work_dir,
                                        [member_rule()], "member-only",
                                        offline=not args.online)
    assert_selection(member_only, [MEMBER])
    assert all(item["package_id"] is None for item in member_only["invocations"]
               if item["crate_name"] == "adler2")

    unmatched, _ = build_with_wrapper(wrapper, rustc, args.cargo, work_dir,
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
    print("PASS: Cargo workspace and registry selection, host filtering, strict unmatched, check report")


if __name__ == "__main__":
    main()
