"""Check exact function/string selection and opt-in skip diagnostics."""

import argparse
import subprocess
import sys
from pathlib import Path

from test_transforms import target_body, instruction_count


SEED = "00112233445566778899aabbccddeeff"
ARGS = ((17, 29), (0, 0), (42, 5))
VARIANTS = {
    "baseline": ("-obf-only-functions=__none__",),
    "only-selected": ("-sub", "-obf-only-functions=selected", "-obf-report-skips"),
    "two-functions": ("-sub", "-obf-only-functions=selected,neighbor"),
    "annotation": ("-obf-only-functions=annotated",),
    "veto": ("-sub", "-obf-only-functions=vetoed", "-obf-report-skips"),
    "one-string": ("-sobf", "-obf-only-functions=__none__",
                   "-sobf-only-globals=secret_a", "-obf-report-skips"),
    "weak-string": ("-sobf", "-obf-only-functions=__none__",
                    "-sobf-only-globals=weak_message", "-obf-report-skips"),
    "missing-string": ("-sobf", "-obf-only-functions=__none__",
                       "-sobf-only-globals=missing_secret", "-obf-report-skips"),
}


def run(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def expanded(ir, baseline, name):
    return instruction_count(target_body(ir, name)) > instruction_count(
        target_body(baseline, name)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    args = parser.parse_args()

    clang = [str(args.clang.resolve())]
    if args.sysroot:
        clang.extend(("-isysroot", str(args.sysroot.resolve())))
    elif sys.platform == "darwin":
        clang.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).stdout.strip()))
    opt = str(args.opt.resolve())
    source = Path(__file__).with_name("selection.c").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    for level in ("O0", "O2"):
        ir_by_name = {}
        stderr_by_name = {}
        output_by_name = {}
        for name, options in VARIANTS.items():
            flags = [item for option in options for item in ("-mllvm", option)]
            flags.extend(("-mllvm", f"-obf-test-seed={SEED}"))
            common = [*clang, f"-{level}", "-fno-discard-value-names", *flags,
                      str(source)]
            ir_path = work_dir / f"{level}-{name}.ll"
            executable = work_dir / f"{level}-{name}"
            if sys.platform == "win32":
                executable = executable.with_suffix(".exe")
            result = run([*common, "-S", "-emit-llvm", "-o", str(ir_path)])
            run([opt, "-passes=verify", "-disable-output", str(ir_path)])
            run([*common, "-o", str(executable)])
            ir_by_name[name] = ir_path.read_text(encoding="utf-8")
            stderr_by_name[name] = result.stderr
            output_by_name[name] = [
                run([str(executable), str(a), str(b)], timeout=10).stdout
                for a, b in ARGS
            ]
            print(f"[{level}] selection variant {name} compiled and ran", flush=True)

        baseline = ir_by_name["baseline"]
        for name, outputs in output_by_name.items():
            assert outputs == output_by_name["baseline"], (
                f"{level}/{name}: selection changed program output"
            )

        only = ir_by_name["only-selected"]
        assert expanded(only, baseline, "selected")
        for name in ("neighbor", "annotated", "vetoed"):
            assert target_body(only, name) == target_body(baseline, name), (
                f"{level}/{name}: function allowlist was not exact"
            )
        assert 'symbol="neighbor" reason=not-selected' in stderr_by_name["only-selected"]

        two = ir_by_name["two-functions"]
        assert expanded(two, baseline, "selected")
        assert expanded(two, baseline, "neighbor")
        assert target_body(two, "annotated") == target_body(baseline, "annotated")

        annotated = ir_by_name["annotation"]
        assert expanded(annotated, baseline, "annotated")
        assert target_body(annotated, "selected") == target_body(baseline, "selected")

        veto = ir_by_name["veto"]
        assert target_body(veto, "vetoed") == target_body(baseline, "vetoed")
        assert 'symbol="vetoed" reason=negative-annotation' in stderr_by_name["veto"]

        one_string = ir_by_name["one-string"]
        assert "selected-secret-alpha-29fdb7" not in one_string
        assert "selected-secret-beta-68a5c1" in one_string
        assert "weak-public-message-0e42c8" in one_string
        assert 'symbol="secret_b" reason=not-selected' in stderr_by_name["one-string"]

        weak = ir_by_name["weak-string"]
        assert "weak-public-message-0e42c8" in weak
        assert 'symbol="weak_message"' in stderr_by_name["weak-string"]
        assert "reason=weak" in stderr_by_name["weak-string"]
        assert ('symbol="missing_secret" reason=not-found'
                in stderr_by_name["missing-string"])
        print(f"[{level}] exact selection and skip reasons passed", flush=True)


if __name__ == "__main__":
    main()
