"""Exercise Cargo unit, integration, and documentation tests from a delivery."""

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def run(command, env, log):
    result = subprocess.run([str(p) for p in command], env=env, capture_output=True,
                            text=True, timeout=300)
    output = result.stdout + result.stderr
    log.write_text(output)
    if result.returncode:
        raise AssertionError(f"{command!r}\n{result.stdout}\n{result.stderr}")
    return output


def verify_tests(output):
    for marker in ("unit-gate-executed", "integration-gate-executed", "Doc-tests cargo_test_gate"):
        if marker not in output:
            raise AssertionError(f"Cargo did not execute {marker}:\n{output}")
    if output.count("test result: ok.") != 3:
        raise AssertionError(f"expected passing unit, integration and doctest harnesses:\n{output}")


def exercise(args, work):
    work.mkdir(parents=True, exist_ok=True)
    fixture = work / "fixture"
    shutil.copytree(Path(__file__).with_name("cargo_test_fixture"), fixture)
    rustc = args.rustc.resolve()
    rustdoc = rustc.with_name("rustdoc.exe" if os.name == "nt" else "rustdoc")
    if not rustdoc.is_file():
        raise AssertionError(f"delivered rustdoc is missing: {rustdoc}")
    env = dict(os.environ)
    for name in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_ENCODED_RUSTFLAGS", "RUSTFLAGS", "CARGO_TARGET_DIR"):
        env.pop(name, None)
    env.update(RUSTC=str(rustc), RUSTDOC=str(rustdoc))
    command = ["test", "--locked", "--offline", "--manifest-path", fixture / "Cargo.toml", "--", "--nocapture"]
    baseline_env = dict(env, CARGO_TARGET_DIR=str(work / "baseline"))
    verify_tests(run(["cargo", *command], baseline_env, work / "baseline.log"))
    config = work / "config.json"
    config.write_text(json.dumps({"version": 1, "rustc": str(rustc), "strict": True,
        "seed": "00112233445566778899aabbccddeeff", "packages": [{
            "name": "cargo-test-gate", "source": "workspace", "targets": ["cargo_test_gate"],
            "passes": ["obf-sub"], "functions": ["cargo_test_probe"]}]}))
    report = work / "report.json"
    verify_tests(run([args.wrapper.resolve(), "--config", config, "--report", report, "--", *command], env,
                     work / "selected.log"))
    data = json.loads(report.read_text())
    if not data.get("strict_passed"):
        raise AssertionError(f"Cargo test did not pass strict selection: {data}")
    print("PASS extracted Cargo unit, integration and documentation tests")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.work_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    context = (tempfile.TemporaryDirectory(prefix="rct-", dir=os.environ.get("RUNNER_TEMP"))
               if os.name == "nt" else nullcontext(evidence))
    with context as scratch:
        work = Path(scratch)
        try:
            exercise(args, work)
        finally:
            if work != evidence:
                # MSVC needs short object/output paths. Retain diagnostic data
                # under --work-dir before removing the short-lived build tree.
                for path in work.rglob("*"):
                    if path.is_file() and path.suffix in (".json", ".jsonl", ".log"):
                        destination = evidence / path.relative_to(work)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)
                (evidence / "scratch-location.json").write_text(json.dumps({
                    "original_build_directory": str(work),
                    "retained_evidence_directory": str(evidence)}, indent=2) + "\n")


if __name__ == "__main__":
    main()
