"""Dependency-free Cargo forwarding and per-target coverage regressions."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


FIXTURE = Path(__file__).resolve().parent / "cargo_wrapper_fixture"


def run(command, *, env, code=0, output=None):
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            timeout=120)
    if output is not None:
        output.with_suffix(".stdout").write_text(result.stdout)
        output.with_suffix(".stderr").write_text(result.stderr)
    if result.returncode != code:
        raise AssertionError(
            f"expected exit {code}, got {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def run_regressions(wrapper, rustc, cargo, work):
    work.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # Wrapper-owned Cargo targets nest below reports; MSVC still encounters
        # MAX_PATH under the extracted gate's already deep work directory.
        with tempfile.TemporaryDirectory(prefix="rcw-", dir=os.environ.get("RUNNER_TEMP")) as temp:
            scratch = Path(temp)
            try:
                _run_regressions(wrapper, rustc, cargo, scratch)
            finally:
                # Keep manifests, event shards, and logs even when a case fails.
                # Report paths retain their original compiler-invocation values.
                shutil.copytree(scratch, work, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("target"))
                (work / "scratch-location.json").write_text(json.dumps(
                    {"original_root": str(scratch), "retained_root": str(work)}) + "\n")
    else:
        _run_regressions(wrapper, rustc, cargo, work)


def _run_regressions(wrapper, rustc, cargo, work):
    fixture = work / "fixture"
    shutil.copytree(FIXTURE, fixture, dirs_exist_ok=True)
    host = next(line.removeprefix("host: ") for line in subprocess.check_output(
        [str(rustc), "-vV"], text=True, timeout=30).splitlines()
                if line.startswith("host: "))
    env = os.environ.copy()
    # This fixture observes wrapper-owned flags, independent of surrounding gates.
    for name in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER",
                 "RUSTC_WORKSPACE_WRAPPER", "RUST_OBF_WRAPPER_MODE",
                 "RUST_OBF_RUNTIME_CONFIG", "RUST_OBF_EVENT_FILE",
                 "OBF_EVENT_DIR", "OBF_EVENT_FILE"):
        env.pop(name, None)

    def invoke(label, command, *, targets=("alpha",), code=0, extra_env=None):
        config = {
            "version": 1, "rustc": str(rustc), "cargo": str(cargo),
            "strict": True, "seed": "00112233445566778899aabbccddeeff",
            "packages": [{"name": "rust-obf-wrapper-fixture", "source": "workspace",
                          "targets": list(targets), "crate_types": ["bin"],
                          "passes": ["obf-sub"], "functions": ["wrapper_probe"]}],
        }
        config_path = work / f"{label}-config.json"
        report_path = work / f"{label}-report.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        command = [str(wrapper), "--config", str(config_path), "--report", str(report_path),
                   "--", command[0], "--manifest-path", str(fixture / "Cargo.toml"),
                   "--release", "--locked", "--offline", *command[1:]]
        result = run(command, env={**env, **(extra_env or {})}, code=code, output=work / label)
        report = json.loads(report_path.read_text())
        assert report["cargo_exit_code"] == 0, report
        assert report["code_artifact"], report
        assert all(item["target"] == host for item in report["invocations"]
                   if item["status"] == "selected"), report
        return report, result

    # Both requested targets compile. Only alpha transforms the exact function;
    # beta must not borrow alpha's success from the package-level summary.
    report, _ = invoke("target-isolation", ["build", "--bins"],
                       targets=("alpha", "beta"), code=2)
    assert not report["strict_passed"], report
    package = report["packages"][0]
    assert package["unmatched_targets"] == [], package
    assert package["unmatched_crate_types"] == [], package
    assert package["pass_summary"]["obf-sub"]["transformed_symbols"] == 1, package
    targets = {target["crate_name"]: target for target in package["targets"]}
    assert targets["alpha"]["strict_passed"], targets
    assert not targets["beta"]["strict_passed"], targets
    assert targets["beta"]["pass_summary"]["obf-sub"]["transformed_symbols"] == 0
    beta = report["invocations"][targets["beta"]["invocation_index"]]
    events = [json.loads(line) for line in Path(beta["event_file"]).read_text().splitlines()]
    assert any(event["raw_name"] == "wrapper_probe" and event["event"] == "skip"
               and event["reason"] == "no-eligible-instructions" for event in events), events

    # Cargo-looking application arguments must neither affect metadata nor prevent
    # the implicit Cargo target, and must reach the application unchanged.
    forwarded = ["hello", "--target", "application-target", "--manifest-path",
                 "missing/Cargo.toml", "--config=missing.toml", "--target-dir", "application-dir"]
    report, result = invoke("run-forwarding", ["run", "--bin", "alpha", "--", *forwarded],
        extra_env={"OBF_EVENT_DIR": str(work / "missing-events-directory"),
                   "OBF_EVENT_FILE": str(work / "missing-events-directory" / "stale.jsonl")})
    assert report["strict_passed"], report
    assert result.stdout == "15\n" + "|".join(forwarded) + "\n", result.stdout

    report, _ = invoke("rustc-forwarding", ["rustc", "--bin", "alpha", "--",
                                           "--cfg", "wrapper_forwarded"])
    assert report["strict_passed"], report
    exe = "alpha.exe" if sys.platform == "win32" else "alpha"
    binary = Path(report["target_dir"]) / host / "release" / exe
    assert run([str(binary)], env=env).stdout == "15\n\nrustc-forwarded\n"

    # An explicit target must remain a Cargo option as well.
    report, result = invoke("run-explicit-target", ["run", "--bin", "alpha",
        "--target", host, "--", "explicit-target"])
    assert report["strict_passed"], report
    assert result.stdout == "15\nexplicit-target\n", result.stdout

    # Requires the packaged library/test. --nocapture belongs to the harness;
    # selecting crate_types=bin also verifies test-harness target classification.
    report, result = invoke("test-forwarding", ["test", "--bin", "alpha", "--",
                                               "--nocapture", "forwarding_test"])
    assert report["strict_passed"], report
    assert "wrapper-test-forwarding" in result.stdout, result.stdout
    assert "1 passed" in result.stdout, result.stdout
    print("PASS: Cargo argument forwarding and per-target strict coverage")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    run_regressions(args.wrapper.resolve(), args.rustc.resolve(), args.cargo,
                    args.work_dir.resolve())


if __name__ == "__main__":
    main()
