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


def source_regressions(wrapper, rustc, cargo, work, host, base_env):
    # Cargo reports both a non-workspace path crate and a Git crate under their
    # own package IDs, even when they have the same package name and version.
    # Keep this fixture local and dependency-free for all native CI platforms.
    root = work / "s"
    workspace = root / "w"
    app = workspace / "a"
    path_dep = root / "p"
    git_dep = root / "g"
    for package in (app, path_dep, git_dep):
        (package / "src").mkdir(parents=True)

    (workspace / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["a"]\nresolver = "2"\n')
    name = "rust-obf-source-probe"
    dependency_manifest = (f'[package]\nname = "{name}"\nversion = "0.1.0"\n'
                           'edition = "2021"\n')
    for package, symbol, increment in ((path_dep, "path_probe", 7),
                                        (git_dep, "git_probe", 17)):
        (package / "Cargo.toml").write_text(dependency_manifest)
        (package / "src" / "lib.rs").write_text(
            f'#[no_mangle]\n#[inline(never)]\n'
            f'pub extern "C" fn {symbol}(value: u32) -> u32 {{\n'
            f'    value.wrapping_add({increment})\n}}\n')

    run(["git", "init", "--quiet", str(git_dep)], env=base_env)
    run(["git", "-C", str(git_dep), "add", "."], env=base_env)
    run(["git", "-C", str(git_dep), "-c", "commit.gpgsign=false",
         "-c", "user.name=Rust Obfuscation Tests",
         "-c", "user.email=rust-obf-tests@example.invalid",
         "commit", "--quiet", "-m", "local Git dependency"], env=base_env)

    (app / "Cargo.toml").write_text(
        '[package]\nname = "rust-obf-sources-app"\nversion = "0.1.0"\n'
        'edition = "2021"\n[dependencies]\n'
        f'path_alias = {{ package = "{name}", path = "../../p" }}\n'
        f'git_alias = {{ package = "{name}", git = "{git_dep.resolve().as_uri()}" }}\n')
    (app / "src" / "main.rs").write_text(
        'fn main() {\n'
        '    let value = std::hint::black_box(8);\n'
        '    println!("{}:{}", path_alias::path_probe(value), '
        'git_alias::git_probe(value));\n}\n')

    env = dict(base_env)
    env["CARGO_HOME"] = str(root / "h")
    manifest = workspace / "Cargo.toml"
    # The initial metadata call fetches only the local file:// Git repository
    # and writes Cargo.lock. Subsequent wrapper builds can be locked/offline.
    metadata = json.loads(run([str(cargo), "metadata", "--format-version", "1",
                               "--manifest-path", str(manifest)], env=env).stdout)
    members = set(metadata["workspace_members"])
    candidates = [package for package in metadata["packages"]
                  if package["name"] == name and package["version"] == "0.1.0"]
    assert len(candidates) == 2, candidates
    path_package = next(package for package in candidates
                        if package["source"] is None)
    git_package = next(package for package in candidates
                       if (package["source"] or "").startswith("git+file://"))
    assert path_package["id"] not in members, metadata["workspace_members"]
    assert git_package["id"] not in members, metadata["workspace_members"]
    assert Path(path_package["manifest_path"]).parent.resolve() == path_dep.resolve()
    assert path_package["id"] != git_package["id"]
    assert (workspace / "Cargo.lock").exists()

    for label, source, package, symbol, exact_id in (
            ("source-path", "path", path_package, "path_probe", False),
            ("source-git", "git", git_package, "git_probe", False),
            ("source-git-id", "git", git_package, "git_probe", True)):
        rule = {"name": name, "version": "0.1.0", "source": source,
                "crate_types": ["rlib"], "passes": ["obf-sub"],
                "functions": [symbol]}
        if exact_id:
            rule["package_id"] = package["id"]
        config_path = work / f"{label}-config.json"
        report_path = work / f"{label}-report.json"
        config_path.write_text(json.dumps({
            "version": 1, "rustc": str(rustc), "cargo": str(cargo),
            "strict": True, "seed": "00112233445566778899aabbccddeeff",
            "packages": [rule]}, indent=2) + "\n")
        command = [str(wrapper), "--config", str(config_path), "--report",
                   str(report_path), "--", "build", "--release", "--locked",
                   "--offline", "--manifest-path", str(manifest)]
        run(command, env=env, output=work / label)
        report = json.loads(report_path.read_text())
        assert report["cargo_exit_code"] == 0 and report["strict_passed"], report
        assert report["code_artifact"] and len(report["packages"]) == 1, report
        chosen = report["packages"][0]
        assert chosen["package_id"] == package["id"], chosen
        assert chosen["compiled"] > 0, chosen
        summary = chosen["pass_summary"]["obf-sub"]
        assert summary["transformed_symbols"] > 0 and summary["transformed_sites"] > 0, summary
        assert any(event["event"] == "effect" and event["kind"] == "function"
                   and event["raw_name"] == symbol for event in chosen["events"]), chosen
        # Dependency aliases name `--extern` flags, but rustc's --crate-name is
        # the package library name for both copies.
        dependency_invocations = [item for item in report["invocations"]
                                  if item["crate_name"] == "rust_obf_source_probe"]
        assert len(dependency_invocations) == 2, report
        selected = [item for item in dependency_invocations
                    if item["status"] == "selected"]
        unselected = [item for item in dependency_invocations
                      if item["status"] == "unselected"]
        assert len(selected) == len(unselected) == 1, report
        assert selected[0]["package_id"] == package["id"], report
        assert selected[0]["target"] == host, report
        assert unselected[0]["package_id"] is None, report
        executable = "rust-obf-sources-app.exe" if sys.platform == "win32" else "rust-obf-sources-app"
        binary = Path(report["target_dir"]) / host / "release" / executable
        assert run([str(binary)], env=env).stdout == "15:25\n", binary

    # A tag and a branch at one commit have distinct Cargo package IDs but
    # share a checkout directory. The rustc wrapper sees only the manifest
    # directory, so even an exact package_id must fail before Cargo compiles.
    run(["git", "-C", str(git_dep), "branch", "same-commit-branch"], env=env)
    run(["git", "-C", str(git_dep), "tag", "same-commit-tag"], env=env)
    alias_workspace = root / "d"
    alias_app = alias_workspace / "a"
    (alias_app / "src").mkdir(parents=True)
    (alias_workspace / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["a"]\nresolver = "2"\n')
    git_uri = git_dep.resolve().as_uri()
    (alias_app / "Cargo.toml").write_text(
        '[package]\nname = "rust-obf-alias-app"\nversion = "0.1.0"\n'
        'edition = "2021"\n[dependencies]\n'
        f'branch_alias = {{ package = "{name}", git = "{git_uri}", '
        'branch = "same-commit-branch" }\n'
        f'tag_alias = {{ package = "{name}", git = "{git_uri}", '
        'tag = "same-commit-tag" }\n')
    (alias_app / "src" / "main.rs").write_text(
        'fn main() { println!("{}", branch_alias::git_probe(8) '
        '+ tag_alias::git_probe(8)); }\n')
    alias_manifest = alias_workspace / "Cargo.toml"
    alias_metadata = json.loads(run([str(cargo), "metadata", "--format-version", "1",
                                     "--manifest-path", str(alias_manifest)], env=env).stdout)
    aliases = [package for package in alias_metadata["packages"]
               if package["name"] == name and
               (package["source"] or "").startswith("git+file://")]
    assert len(aliases) == 2, aliases
    assert aliases[0]["id"] != aliases[1]["id"], aliases
    assert Path(aliases[0]["manifest_path"]).parent.resolve() == \
        Path(aliases[1]["manifest_path"]).parent.resolve(), aliases
    tag_package = next(package for package in aliases
                       if "?tag=same-commit-tag" in package["source"])
    alias_config = work / "source-git-alias-config.json"
    alias_report = work / "source-git-alias-report.json"
    alias_config.write_text(json.dumps({
        "version": 1, "rustc": str(rustc), "cargo": str(cargo),
        "strict": True, "seed": "00112233445566778899aabbccddeeff",
        "packages": [{"name": name, "version": "0.1.0", "source": "git",
                      "package_id": tag_package["id"], "crate_types": ["rlib"],
                      "passes": ["obf-sub"], "functions": ["git_probe"]}]},
        indent=2) + "\n")
    result = run([str(wrapper), "--config", str(alias_config), "--report",
                  str(alias_report), "--", "build", "--release", "--locked",
                  "--offline", "--manifest-path", str(alias_manifest)],
                 env=env, code=1, output=work / "source-git-alias")
    assert "same manifest directory" in result.stderr, result.stderr
    assert "Compiling" not in result.stderr, result.stderr
    assert not alias_report.exists(), alias_report
    assert not alias_report.with_suffix(".rust-obf-events").exists(), alias_report

    print("PASS: local Git and non-workspace path package selection")
    print("PASS: distinct Git refs at one checkout fail before compilation")


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
    source_regressions(wrapper, rustc, cargo, work, host, env)
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
