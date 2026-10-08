"""Compile and run the Clang-exposed obfuscation passes at O0 and O2.

The transforms use random choices, so this checks invariant IR effects and
program behavior instead of matching a particular sequence of instructions.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


MESSAGE = "obfuscation-smoke-marker-79e6b1"
MASK = (1 << 32) - 1
CASES = ((17, 29), (0, 0), (1, 2), (42, 5), (0xFFFFFFFF, 1))
VARIANTS = {
    "baseline": (),
    "sobf": ("-sobf",),
    "sub": ("-sub", "-sub_loop=1"),
    "split": ("-split", "-split_num=2"),
    "bcf": ("-bcf", "-bcf_prob=100"),
    "fla": ("-fla",),
    "combined": ("-sobf", "-sub", "-split", "-bcf", "-bcf_prob=100", "-fla"),
}


def run(command, timeout=120):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"Timed out after {timeout}s: {command!r}") from error
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def expected_value(a, b):
    x = ((a + b) & MASK) ^ (a | 0xA5A5)
    y = ((a & b) + 11) & MASK
    if x & 1:
        sink = x
        x = (x * 3 - y) & MASK
    else:
        sink = y
        x = (x * 2 + y) & MASK
    if b & 2:
        sink = x
        x ^= y | 7
    else:
        sink = y
        x = (x + (a ^ 13)) & MASK
    return (x + sink) & MASK


def expected(a, b):
    return f"{expected_value(a, b)}:{MESSAGE}\n"


def target_body(ir, function="transform_target"):
    lines = ir.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^define\b.*@{re.escape(function)}\(", line):
            for end in range(index + 1, len(lines)):
                if lines[end] == "}":
                    return "\n".join(lines[index + 1 : end])
            break
    raise AssertionError(f"{function} definition missing from emitted IR")


def instruction_count(body):
    return len(re.findall(r"^\s+%[^=\n]+\s=\s", body, re.MULTILINE))


def branch_count(body):
    return len(re.findall(r"^\s+br\s", body, re.MULTILINE))


def check_function_effect(name, body, base_body, ir=""):
    if name == "sub":
        assert instruction_count(body) > instruction_count(base_body), (
            "substitution did not expand arithmetic"
        )
    elif name == "split":
        assert branch_count(body) > branch_count(base_body), (
            "split did not add basic-block branches"
        )
    elif name == "bcf":
        assert re.search(r"^@x(?:\.\d+)?\s*=\s*common\b", ir, re.MULTILINE), (
            "bogus control flow did not emit an opaque-predicate global"
        )
        assert "urem i32" in body and branch_count(body) > branch_count(base_body), (
            "bogus control flow did not alter the function's branches"
        )
    elif name == "fla":
        assert "switch i32" not in base_body, "fixture baseline unexpectedly has a switch"
        assert "switch i32" in body, "flattening did not add a dispatcher switch"


def check_effect(name, ir, baseline):
    if name == "sobf":
        assert MESSAGE in baseline, "baseline IR must retain the fixture string"
        assert MESSAGE not in ir, "string obfuscation left plaintext in IR"
        assert "@llvm.global_ctors" in ir, "string decoder startup is missing"
        assert ".datadiv_decode" in ir, "string decoder function is missing"
    else:
        check_function_effect(name, target_body(ir), target_body(baseline), ir)


def check_combined_effect(ir, baseline):
    check_effect("sobf", ir, baseline)
    body = target_body(ir)
    assert re.search(r"^@x(?:\.\d+)?\s*=\s*common\b", ir, re.MULTILINE), (
        "combined pipeline did not emit BCF opaque-predicate globals"
    )
    assert "urem i32" in body, "combined pipeline did not apply BCF to target"
    assert "switch i32" in body, "combined pipeline lacks flatten dispatcher"
    assert re.search(r"(?m)^[^\s;][^:\n]*\.split[^:\n]*:", body), (
        "combined pipeline lacks a split basic block"
    )
    # Substitution has no stable syntax marker once BCF and flattening also
    # insert arithmetic. Its independent variant checks the expansion.


def check_annotations(clang, opt, work_dir, level):
    source = Path(__file__).with_name("annotations.c").resolve()
    for name in ("sub", "split", "bcf", "fla"):
        emitted = {}
        # The numeric option makes annotation-only BCF deterministic; -bcf
        # itself is deliberately absent from the positive-only compilation.
        common_options = ("-bcf_prob=100",) if name == "bcf" else ()
        for global_flag in (False, True):
            variant = "global" if global_flag else "annotation-only"
            stem = work_dir / f"{level}-{name}-{variant}"
            ir_path = stem.with_suffix(".ll")
            executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
            options = (*common_options, *((f"-{name}",) if global_flag else ()))
            flags = [item for option in options for item in ("-mllvm", option)]
            command = [
                *clang,
                f"-{level}",
                "-fno-discard-value-names",
                f"-DTEST_{name.upper()}",
                *flags,
                str(source),
            ]
            print(f"[{level}] compile {name} {variant}", flush=True)
            run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
            run([opt, "-passes=verify", "-disable-output", str(ir_path)])
            run([*command, "-o", str(executable)])
            emitted[variant] = ir_path.read_text(encoding="utf-8")
            for a, b in CASES:
                actual = run([str(executable), str(a), str(b)], timeout=10)
                value = expected_value(a, b)
                assert actual == f"{value}:{value}\n", (
                    f"{level}/{name}/{variant}({a}, {b}): {actual!r}"
                )

        plain = emitted["annotation-only"]
        with_global = emitted["global"]
        check_function_effect(
            name,
            target_body(plain, "positive"),
            target_body(plain, "negative"),
            plain,
        )
        assert target_body(with_global, "negative") == target_body(
            plain, "negative"
        ), f"{name} ignored the no{name} annotation under the global flag"
        print(f"[{level}] {name} annotation-only and negative override passed", flush=True)


def check_indirectbr(clang, opt, work_dir, level):
    source = Path(__file__).with_name("indirectbr.c").resolve()
    stem = work_dir / f"{level}-indirectbr"
    ir_path = stem.with_suffix(".ll")
    executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
    command = [*clang, f"-{level}", "-mllvm", "-fla", str(source)]
    run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
    run([opt, "-passes=verify", "-disable-output", str(ir_path)])
    if level == "O0":
        assert "indirectbr" in ir_path.read_text(encoding="utf-8"), (
            "computed-goto fixture did not exercise indirectbr at O0"
        )
    run([*command, "-o", str(executable)])
    run([str(executable)], timeout=10)
    print(f"[{level}] flattening leaves computed goto valid", flush=True)


def check_musttail(clang, opt, work_dir, level):
    source = Path(__file__).with_name("musttail.c").resolve()
    for name, options in {
        "split": ("-split", "-split_num=2"),
        "bcf": ("-bcf", "-bcf_prob=100"),
        "fla": ("-fla",),
    }.items():
        stem = work_dir / f"{level}-musttail-{name}"
        ir_path = stem.with_suffix(".ll")
        executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
        flags = [item for option in options for item in ("-mllvm", option)]
        command = [*clang, f"-{level}", *flags, str(source)]
        run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
        ir = ir_path.read_text(encoding="utf-8")
        assert "musttail call" in ir, "musttail fixture did not retain the call"
        run([opt, "-passes=verify", "-disable-output", str(ir_path)])
        run([*command, "-o", str(executable)])
        run([str(executable)], timeout=10)
        print(f"[{level}] {name} preserves musttail", flush=True)


def check_eh(clang, opt, work_dir, level):
    source = Path(__file__).with_name("eh.cpp").resolve()
    for target, extra_flags, pad in (
        ("native", (), "landingpad"),
        ("windows", ("--target=x86_64-pc-windows-msvc", "-fexceptions",
                     "-fcxx-exceptions"), "catchpad"),
    ):
        ir_path = work_dir / f"{level}-bcf-{target}-eh.ll"
        command = [
            *clang, *extra_flags, "-x", "c++", f"-{level}",
            "-mllvm", "-bcf", "-mllvm", "-bcf_prob=100",
            "-S", "-emit-llvm", str(source), "-o", str(ir_path),
        ]
        run(command)
        ir = ir_path.read_text(encoding="utf-8")
        assert pad in ir, f"{target} exception fixture did not exercise {pad}"
        run([opt, "-passes=verify", "-disable-output", str(ir_path)])
        print(f"[{level}] bogus control flow preserves {target} EH edges", flush=True)


def check_cross_tu_strings(clang, clangxx, opt, work_dir, level):
    fixture_dir = Path(__file__).resolve().parent
    objects = []
    for unit, private_marker in (
        ("a", "cross-tu-private-a-marker"),
        ("b", "cross-tu-private-b-marker"),
    ):
        stem = work_dir / f"{level}-odr-string-{unit}"
        ir_path = stem.with_suffix(".ll")
        object_path = stem.with_suffix(".o")
        command = [
            *clangxx, f"-{level}", "-std=c++17", "-mllvm", "-sobf",
            str(fixture_dir / f"odr_string_{unit}.cpp"),
        ]
        run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
        run([opt, "-passes=verify", "-disable-output", str(ir_path)])
        run([*command, "-c", "-o", str(object_path)])
        ir = ir_path.read_text(encoding="utf-8")
        assert 'c"cross-tu-odr-marker\\00"' in ir, (
            f"{level}/{unit}: linker-coalesced ODR string was encoded"
        )
        assert private_marker not in ir and ".datadiv_decode" in ir, (
            f"{level}/{unit}: private literal was not encoded"
        )
        if unit == "a":
            assert "cross-tu-export-marker" not in ir, (
                f"{level}: exported string was not encoded"
            )
            assert re.search(r"(?m)^@exported_message\s*=", ir), (
                f"{level}: exported string symbol was renamed"
            )
        objects.append(str(object_path))

    executable = work_dir / f"{level}-odr-strings"
    run([
        *clang, f"-{level}", str(fixture_dir / "odr_string_main.c"),
        *objects, "-o", str(executable),
    ])
    assert run([str(executable)], timeout=10) == "cross-tu strings passed\n", (
        f"{level}: string decoding or exported symbol failed across translation units"
    )
    print(f"[{level}] cross-TU ODR, exported, and private strings passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    args = parser.parse_args()

    source = Path(__file__).with_name("smoke.c").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    clang = [str(args.clang.resolve())]
    if args.sysroot:
        clang.extend(("-isysroot", str(args.sysroot.resolve())))
    elif sys.platform == "darwin":
        sdk = run(["xcrun", "--show-sdk-path"]).strip()
        clang.extend(("-isysroot", sdk))
    opt = str(args.opt.resolve())
    clangxx = [str(args.clang.resolve().with_name("clang++")), *clang[1:]]

    for level in ("O0", "O2"):
        ir_by_variant = {}
        output_by_variant = {}
        for name, options in VARIANTS.items():
            stem = work_dir / f"{level}-{name}"
            ir_path = stem.with_suffix(".ll")
            executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
            flags = [item for option in options for item in ("-mllvm", option)]
            common = [*clang, f"-{level}", "-fno-discard-value-names", *flags, str(source)]

            print(f"[{level}] compile {name}", flush=True)
            run([*common, "-S", "-emit-llvm", "-o", str(ir_path)])
            run([opt, "-passes=verify", "-disable-output", str(ir_path)])
            run([*common, "-o", str(executable)])
            ir_by_variant[name] = ir_path.read_text(encoding="utf-8")

            outputs = []
            for a, b in CASES:
                actual = run([str(executable), str(a), str(b)], timeout=10)
                oracle = expected(a, b)
                assert actual == oracle, (
                    f"{level}/{name}({a}, {b}): {actual!r} != {oracle!r}"
                )
                outputs.append(actual)
            output_by_variant[name] = outputs

        baseline = ir_by_variant["baseline"]
        for name in ("sobf", "sub", "split", "bcf", "fla"):
            check_effect(name, ir_by_variant[name], baseline)
            assert output_by_variant[name] == output_by_variant["baseline"]
        check_combined_effect(ir_by_variant["combined"], baseline)
        assert output_by_variant["combined"] == output_by_variant["baseline"]
        print(f"[{level}] all transform effects and runtime results passed", flush=True)
        check_annotations(clang, opt, work_dir, level)
        check_indirectbr(clang, opt, work_dir, level)
        check_musttail(clang, opt, work_dir, level)
        check_eh(clang, opt, work_dir, level)
        check_cross_tu_strings(clang, clangxx, opt, work_dir, level)


if __name__ == "__main__":
    main()
