#!/usr/bin/env python3
"""Qualify selected-crate LTO placement against linked Rust 1.99 artifacts."""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path


FIXTURE = Path(__file__).with_name("lto_fixture")
SEED = "00112233445566778899aabbccddeeff"
OTHER_SEED = "ffeeddccbbaa99887766554433221100"
MARKER = "m5-selected-string-583d29"
EXPECTED = ("207605:207605\n161426:161426\n207623:207623\n"
            "161440:161440\n" + MARKER + "\n12\n")
CHOSEN = "rust-obf-lto-chosen"


def run(command, *, env=None, code=0, timeout=600):
    result = subprocess.run(command, env=env, text=True, capture_output=True,
                            timeout=timeout)
    if result.returncode != code:
        raise AssertionError(
            f"exit {result.returncode}, expected {code}: {command!r}\n"
            f"stdout:\n{result.stdout[-3000:]}\n"
            f"stderr:\n{result.stderr[-3000:]}")
    return result


def host_triple(rustc):
    info = run([str(rustc), "-vV"], timeout=30).stdout
    assert re.search(r"(?m)^release: 1\.99\.", info), info
    assert re.search(r"(?m)^LLVM version: 23\.", info), info
    match = re.search(r"(?m)^host: (.+)$", info)
    assert match, info
    return match.group(1)


def profile_options(lto, cgu, level):
    lto_value = "false" if lto == "false" else f'"{lto}"'
    return ["--config", f"profile.release.lto={lto_value}",
            "--config", f"profile.release.codegen-units={cgu}",
            "--config", f"profile.release.opt-level={level}"]


def cargo_args(manifest, lto, cgu, level):
    return ["build", "--release", "--locked", "--offline",
            "--manifest-path", str(manifest), "-p", "rust-obf-lto-app",
            *profile_options(lto, cgu, level)]


def app_path(target_dir, host):
    suffix = ".exe" if os.name == "nt" else ""
    return target_dir / host / "release" / f"rust-obf-lto-app{suffix}"


def llvm_tool_path(objdump, name):
    suffix = ".exe" if os.name == "nt" else ""
    return objdump.with_name(name + suffix)


def saved_ir_env(base=None):
    """Keep the IR from the actual Cargo invocation for independent verification."""
    env = dict(os.environ if base is None else base)
    if "CARGO_ENCODED_RUSTFLAGS" in env:
        prior = env["CARGO_ENCODED_RUSTFLAGS"]
        env["CARGO_ENCODED_RUSTFLAGS"] = (
            prior + ("\x1f" if prior else "") + "-C\x1fsave-temps=yes")
    else:
        env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") +
                            " -C save-temps=yes").strip()
    return env


def verify_emitted_ir(opt, target_dir, host, *, selected_crate):
    deps = target_dir / host / "release" / "deps"
    bitcode = sorted(deps.glob("*.rcgu.bc"))
    assert bitcode, f"Cargo emitted no saved LLVM IR in {deps}"
    assert any(path.name.startswith(selected_crate + "-") for path in bitcode), (
        selected_crate, bitcode)
    for path in bitcode:
        run([str(opt), "-passes=verify", "-disable-output", str(path)],
            timeout=30)


def instructions(objdump, binary, symbol):
    raw = "_" + symbol if platform.system() == "Darwin" else symbol
    dump = run([str(objdump), f"--disassemble-symbols={raw}",
                "--no-show-raw-insn", str(binary)], timeout=30).stdout
    assert f"<{raw}>:" in dump, (binary, raw)
    lines = re.findall(r"(?m)^\s*[0-9a-f]+:\s+(.+)$", dump)
    assert lines, (binary, raw)
    # Linked addresses change when a different sized chosen function shifts
    # later code. Keep relative symbol offsets and all machine operands.
    return [re.sub(r"0x[0-9a-f]+(?= <)", "ADDR", line) for line in lines]


def rule(pass_name, global_name=None):
    item = {"name": CHOSEN, "source": "workspace",
            "targets": [CHOSEN], "crate_types": ["rlib"],
            "passes": [pass_name]}
    if pass_name != "obf-string":
        item["functions"] = ["lto_global" if pass_name == "obf-global-access"
                             else "lto_chosen"]
    if pass_name == "obf-const":
        item["constants"] = ["i64:0x5a17"]
    if pass_name == "obf-global-access":
        assert global_name
        item["globals"] = [global_name]
    return item


def config(rustc, pass_name, seed=SEED, global_name=None):
    return {"version": 1, "rustc": str(rustc), "seed": seed,
            "strict": True, "packages": [rule(pass_name, global_name)]}


def baseline(cargo, rustc, host, opt, manifest, work_dir, label, lto, cgu,
             level):
    target = work_dir / label / "baseline-target"
    env = saved_ir_env({**os.environ, "RUSTC": str(rustc),
                        "CARGO_TARGET_DIR": str(target)})
    run([cargo, *cargo_args(manifest, lto, cgu, level), "--target", host],
        env=env)
    verify_emitted_ir(opt, target, host, selected_crate="rust_obf_lto_chosen")
    binary = app_path(target, host)
    assert run([str(binary)], timeout=30).stdout == EXPECTED, binary
    return binary


def selected(wrapper, rustc, host, opt, manifest, work_dir, label, pass_name,
             lto, cgu, level, *, reuse_target=None, seed=SEED,
             expected_code=0, global_name=None):
    directory = work_dir / label
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.json"
    config_path.write_text(json.dumps(config(rustc, pass_name, seed,
                                              global_name)) + "\n")
    report_path = directory / "report.json"
    command = [str(wrapper), "--config", str(config_path),
               "--report", str(report_path)]
    if reuse_target:
        command.extend(("--reuse-target-dir", str(reuse_target)))
    command.extend(("--", *cargo_args(manifest, lto, cgu, level)))
    result = run(command, env=saved_ir_env(), code=expected_code)
    report = json.loads(report_path.read_text())
    binary = app_path(Path(report["target_dir"]), host)
    if expected_code == 0:
        verify_emitted_ir(opt, Path(report["target_dir"]), host,
                          selected_crate="rust_obf_lto_chosen")
    return report, binary, result


def check_report(report, pass_name):
    assert report["code_artifact"] and report["strict_passed"], report
    package, = report["packages"]
    summary = package["pass_summary"][pass_name]
    assert summary["transformed_symbols"] > 0, summary
    assert summary["transformed_sites"] > 0, summary
    short_pass = {"obf-string": "sobf", "obf-split": "split",
                  "obf-bcf": "bcf", "obf-fla": "fla", "obf-sub": "sub",
                  "obf-const": "constenc",
                  "obf-global-access": "gai"}[pass_name]
    primary_kind = ("global" if pass_name in
                    ("obf-string", "obf-global-access") else "function")
    effects = [event for event in package["events"]
               if event["event"] == "effect" and event["pass"] == short_pass
               and event["kind"] == primary_kind]
    assert len(effects) == 1 and summary["transformed_symbols"] == 1, (
        pass_name, effects, summary)
    assert effects[0]["count"] == summary["transformed_sites"], (
        pass_name, effects, summary)
    if pass_name not in ("obf-string", "obf-global-access"):
        assert effects[0]["raw_name"] == "lto_chosen", effects
    elif pass_name == "obf-global-access":
        assert effects[0]["raw_name"] and effects[0]["kind"] == "global", effects
    else:
        assert effects[0]["kind"] == "global", effects
    selected_invocations = [item for item in report["invocations"]
                            if item["status"] == "selected"]
    assert len(selected_invocations) == 1, selected_invocations
    assert selected_invocations[0]["crate_name"] == "rust_obf_lto_chosen"
    for item in report["invocations"]:
        if item["crate_name"] in ("rust_obf_lto_plain", "rust_obf_lto_app"):
            assert item["status"] == "unselected", item


def check_machine(objdump, ordinary, transformed, *, symbol="lto_chosen"):
    assert instructions(objdump, ordinary, symbol) != \
           instructions(objdump, transformed, symbol), symbol
    assert instructions(objdump, ordinary, "lto_plain") == \
           instructions(objdump, transformed, "lto_plain"), \
           "unselected dependency's linked instructions changed"


def private_state_name(llvm_ar, llvm_dis, ordinary, work_dir):
    archives = list((ordinary.parent / "deps").glob(
        "librust_obf_lto_chosen-*.rlib"))
    assert len(archives) == 1, archives
    archive = archives[0]
    members = run([str(llvm_ar), "t", str(archive)], timeout=30).stdout
    objects = [name for name in members.splitlines() if name.endswith(".rcgu.o")]
    assert objects, members
    found = set()
    for index, member in enumerate(objects):
        bitcode = work_dir / f"state-discovery-{index}.bc"
        with bitcode.open("wb") as output:
            result = subprocess.run([str(llvm_ar), "p", str(archive), member],
                                    stdout=output, stderr=subprocess.PIPE,
                                    timeout=30)
        assert result.returncode == 0, result.stderr
        ir = run([str(llvm_dis), "-o", "-", str(bitcode)],
                 timeout=30).stdout
        found.update(re.findall(
            r"(?m)^@([^\s]+STATE[^\s]*) = internal .* global i64 7,", ir))
    assert len(found) == 1, found
    return found.pop()


def check_incremental(wrapper, rustc, host, opt, objdump, work_dir):
    directory = work_dir / "incremental"
    fixture = directory / "fixture"
    if fixture.exists():
        shutil.rmtree(fixture)
    shutil.copytree(FIXTURE, fixture)
    manifest = fixture / "Cargo.toml"
    target = directory / "target"
    first, binary, _ = selected(wrapper, rustc, host, opt, manifest, work_dir,
                                "incremental/first", "obf-sub", "thin", 4,
                                2, reuse_target=target)
    check_report(first, "obf-sub")
    first_code = instructions(objdump, binary, "lto_chosen")

    cached, _, _ = selected(wrapper, rustc, host, opt, manifest, work_dir,
                            "incremental/cached", "obf-sub", "thin", 4,
                            2, reuse_target=target, expected_code=2)
    assert not cached["code_artifact"] and not cached["strict_passed"]
    assert cached["coverage_status"] == "no-selected-crate-compiled", cached

    source = fixture / "chosen/src/lib.rs"
    future = time.time() + 3
    os.utime(source, (future, future))
    rebuilt, binary, _ = selected(wrapper, rustc, host, opt, manifest, work_dir,
                                  "incremental/rebuilt", "obf-sub", "thin",
                                  4, 2, reuse_target=target)
    check_report(rebuilt, "obf-sub")
    assert instructions(objdump, binary, "lto_chosen") == first_code
    assert run([str(binary)], timeout=30).stdout == EXPECTED

    changed = directory / "changed-config.json"
    changed.write_text(json.dumps(config(rustc, "obf-sub", OTHER_SEED)) + "\n")
    rejected = run([str(wrapper), "--config", str(changed), "--report",
                    str(directory / "changed-report.json"),
                    "--reuse-target-dir", str(target), "--",
                    *cargo_args(manifest, "thin", 4, 2)], code=1)
    assert "different build identity" in rejected.stderr
    print("PASS incremental: fresh effects, honest no-op, stable rebuild, seed guard")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    wrapper = args.wrapper.resolve()
    rustc = args.rustc.resolve()
    objdump = args.objdump.resolve()
    opt = args.opt.resolve()
    llvm_ar = llvm_tool_path(objdump, "llvm-ar")
    llvm_dis = llvm_tool_path(objdump, "llvm-dis")
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    host = host_triple(rustc)
    manifest = FIXTURE / "Cargo.toml"
    baselines = {}

    for lto in ("off", "false", "thin", "fat"):
        for cgu in (1, 4):
            for level in (2, 3):
                label = f"{lto}-cgu{cgu}-O{level}"
                ordinary = baseline(args.cargo, rustc, host, opt, manifest,
                                    work_dir, label, lto, cgu, level)
                baselines[(lto, cgu, level)] = ordinary
                report, transformed, _ = selected(
                    wrapper, rustc, host, opt, manifest, work_dir,
                    label + "/sub", "obf-sub", lto, cgu, level)
                check_report(report, "obf-sub")
                assert run([str(transformed)], timeout=30).stdout == EXPECTED
                check_machine(objdump, ordinary, transformed)
                print(f"PASS {label}: one selected effect, final code, plain isolation")

    for lto in ("thin", "fat"):
        ordinary = baselines[(lto, 4, 2)]
        for pass_name in ("obf-split", "obf-bcf", "obf-fla", "obf-const"):
            report, transformed, _ = selected(
                wrapper, rustc, host, opt, manifest, work_dir,
                f"{lto}-cgu4-O2/{pass_name}", pass_name, lto, 4, 2)
            check_report(report, pass_name)
            assert run([str(transformed)], timeout=30).stdout == EXPECTED
            check_machine(objdump, ordinary, transformed)
            print(f"PASS {lto} {pass_name}: final code and plain isolation")

        report, transformed, _ = selected(
            wrapper, rustc, host, opt, manifest, work_dir,
            f"{lto}-cgu4-O2/obf-string", "obf-string", lto, 4, 2)
        check_report(report, "obf-string")
        assert run([str(transformed)], timeout=30).stdout == EXPECTED
        assert MARKER.encode() in ordinary.read_bytes()
        assert MARKER.encode() not in transformed.read_bytes()
        assert instructions(objdump, ordinary, "lto_plain") == \
               instructions(objdump, transformed, "lto_plain")
        print(f"PASS {lto} obf-string: final plaintext absent and plain isolation")

        state = private_state_name(llvm_ar, llvm_dis, ordinary, work_dir)
        report, transformed, _ = selected(
            wrapper, rustc, host, opt, manifest, work_dir,
            f"{lto}-cgu4-O2/obf-global-access", "obf-global-access",
            lto, 4, 2, global_name=state)
        check_report(report, "obf-global-access")
        assert run([str(transformed)], timeout=30).stdout == EXPECTED
        check_machine(objdump, ordinary, transformed, symbol="lto_global")
        print(f"PASS {lto} obf-global-access: final code and plain isolation")

    check_incremental(wrapper, rustc, host, opt, objdump, work_dir)


if __name__ == "__main__":
    main()
