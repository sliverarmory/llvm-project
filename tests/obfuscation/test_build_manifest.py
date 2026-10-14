"""Check Clang build-wide strict selection, parallel shards, and failed builds."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import uuid


LAUNCHER = Path(__file__).resolve().parents[2] / "tools/clang-obfuscation/clang_obf.py"


def run(command, expected=0, env=None):
    result = subprocess.run(list(map(str, command)), capture_output=True, text=True,
                            timeout=120, env=env)
    assert result.returncode == expected, (command, result.returncode, result.stdout, result.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", required=True, type=Path)
    parser.add_argument("--opt", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    work = args.work_dir.resolve() / uuid.uuid4().hex[:12]
    work.mkdir(parents=True)
    clang = args.clang.resolve()
    host_flags = (["-isysroot", run(["xcrun", "--show-sdk-path"]).stdout.strip()]
                  if sys.platform == "darwin" else [])
    opt = args.opt.resolve()
    sources = []
    for name, body in (("alpha", "return (x + 19u) ^ (x | 5u);"),
                       ("beta", "return (x ^ 77u) + (x & 31u);"),
                       ("unchanged", "return x;")):
        path = work / f"{name}.c"
        path.write_text(f"unsigned {name}(unsigned x) {{ {body} }}\n")
        sources.append(path)
    main_source = work / "main.c"
    main_source.write_text("unsigned alpha(unsigned), beta(unsigned);\n"
                           "int main(void) { return alpha(7) + beta(8) != 106; }\n")
    config = {"version": 1, "strict": True, "passes": ["obf-sub"],
              "functions": ["alpha", "beta"], "seed": "00112233445566778899aabbccddeeff"}
    config_path = work / "config.json"

    def start(name, settings=config):
        session = work / name
        config_path.write_text(json.dumps(settings))
        run([sys.executable, LAUNCHER, "start", "--config", config_path, "--session", session])
        return session

    def compile_file(session, source):
        output = session / (source.stem + ".ll")
        run([sys.executable, LAUNCHER, "compile", "--session", session, "--", clang,
             *host_flags, "-O0", "-S", "-emit-llvm", source, "-o", output])
        run([opt, "-passes=verify", "-disable-output", output])

    session = start("parallel")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda source: compile_file(session, source), sources[:2]))
    run([sys.executable, LAUNCHER, "finish", "--session", session])
    report = json.loads((session / "report.json").read_text())
    assert report["strict_passed"] and len(report["invocations"]) == 2, report
    assert report["pass_summary"]["obf-sub"]["transformed_symbols"] == 2, report
    binary = work / ("manifest.exe" if sys.platform == "win32" else "manifest")
    run([clang, *host_flags, session / "alpha.ll", session / "beta.ll", main_source, "-o", binary])
    run([binary])
    print("PASS Clang manifest: parallel translation units, verified IR, linked runtime")

    # A single driver invocation may spawn several cc1 processes: their event
    # files must stay separate rather than racing on one shared JSON stream.
    session = start("multi-source-driver")
    run([sys.executable, LAUNCHER, "compile", "--session", session, "--", clang,
         *host_flags, "-O0", *sources[:2], main_source, "-o", binary])
    run([sys.executable, LAUNCHER, "finish", "--session", session])
    run([binary])
    report = json.loads((session / "report.json").read_text())
    assert len({e["event_file"] for e in report["events"]}) >= 2, report

    for name in ("absent", "unchanged"):
        session = start(name, {**config, "functions": ["alpha", name]})
        for source in (sources[0], sources[2]):
            compile_file(session, source)
        run([sys.executable, LAUNCHER, "finish", "--session", session], expected=2)
        summary = json.loads((session / "report.json").read_text())["pass_summary"]["obf-sub"]
        field = "absent_selectors" if name == "absent" else "untransformed_selectors"
        assert summary[field] == [{"kind": "function", "raw_name": name}], summary
    print("PASS Clang manifest: absent and explicitly skipped symbols fail strict selection")

    message = work / "message.c"
    message.write_text('static const char secret[] = "manifest-literal";\n'
                       'extern int puts(const char *);\n'
                       'int show(void) { return puts(secret); }\n')
    session = start("global-selection", {"version": 1, "strict": True,
                    "passes": ["obf-string"], "string_globals": ["secret"]})
    for source in (message, sources[0]):
        compile_file(session, source)
    run([sys.executable, LAUNCHER, "finish", "--session", session])
    report = json.loads((session / "report.json").read_text())
    assert report["pass_summary"]["obf-string"]["transformed_symbols"] == 1, report
    assert any(e.get("reason") == "not-found" for e in report["events"]), report
    assert report["strict_passed"], report
    print("PASS Clang manifest: global absent in another translation unit does not fail the build")

    for strict in (False, True):
        session = start(f"empty-{strict}", {**config, "strict": strict})
        run([sys.executable, LAUNCHER, "finish", "--session", session], expected=2 if strict else 0)
        report = json.loads((session / "report.json").read_text())
        assert not report["strict_passed"] and report["coverage_status"] == "no-compiler-events"
    session = start("unfinished")
    (session / "invocations" / "killed").mkdir()
    (session / "invocations" / "killed" / "pending").touch()
    run([sys.executable, LAUNCHER, "finish", "--session", session], expected=1)
    session = start("failed", {**config, "strict": False})
    run([sys.executable, LAUNCHER, "compile", "--session", session, "--", clang,
         "-c", work / "missing.c"], expected=1)
    run([sys.executable, LAUNCHER, "finish", "--session", session], expected=1)
    print("PASS Clang manifest: no-op, interrupted, and failed builds cannot claim coverage")


if __name__ == "__main__":
    main()
