"""Check that splitting survives O2 in final code and preserves behavior."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from test_transforms import CASES, expected


SEED = "00112233445566778899aabbccddeeff"


def run(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def instructions(objdump, executable):
    for symbol in ("transform_target", "_transform_target"):
        output = run([
            objdump, "--disassemble", "--no-show-raw-insn",
            f"--disassemble-symbols={symbol}", str(executable),
        ])
        marker = re.search(rf"(?m)^\s*[0-9a-fA-F]+ <{symbol}>:$", output)
        if not marker:
            continue
        body = output[marker.end():]
        body = re.split(r"(?m)^\s*[0-9a-fA-F]+ <[^>]+>:$", body, maxsplit=1)[0]
        names = re.findall(r"(?m)^\s*[0-9a-fA-F]+:\s+([a-z][a-z0-9.]*)\b", body)
        if names:
            return names
    raise AssertionError(f"transform_target missing from {executable} disassembly")


def conditional_branches(names):
    return sum(
        name.startswith("b.") or name in ("cbz", "cbnz", "tbz", "tbnz")
        or (name.startswith("j") and name not in ("jmp", "jmpq"))
        for name in names
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    clang = [str(args.clang.resolve())]
    if sys.platform == "darwin":
        clang.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).strip()))
    opt = str(args.opt.resolve())
    objdump = str(args.objdump.resolve())
    source = Path(__file__).with_name("smoke.c").resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    built = {}
    for name, flags in (
        ("baseline", ()),
        ("split", (
            "-mllvm", "-split", "-mllvm", "-split_num=2",
            "-mllvm", f"-obf-test-seed={SEED}",
            "-mllvm", "-obf-only-functions=transform_target",
        )),
    ):
        stem = work_dir / f"O2-{name}"
        executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
        ir_path = stem.with_suffix(".ll")
        command = [*clang, "-O2", "-fno-discard-value-names", *flags, str(source)]
        run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
        run([opt, "-passes=verify", "-disable-output", str(ir_path)])
        export = (["-Xlinker", "/EXPORT:transform_target"]
                  if sys.platform == "win32" else [])
        run([*command, *export, "-o", str(executable)])
        for a, b in CASES:
            actual = run([str(executable), str(a), str(b)], timeout=10)
            assert actual == expected(a, b), f"{name}({a}, {b}): {actual!r}"
        built[name] = (ir_path, executable)

    split_ir = built["split"][0].read_text(encoding="utf-8")
    assert re.search(r"@\.obf\.split\.state(?:\.\d+)? = private global i32", split_ir)
    assert "load volatile i32, ptr @.obf.split.state" in split_ir

    baseline_code = instructions(objdump, built["baseline"][1])
    split_code = instructions(objdump, built["split"][1])
    assert split_code != baseline_code, "O2 split target has identical final code"
    assert conditional_branches(split_code) > conditional_branches(baseline_code), (
        "O2 split target has no extra conditional machine branch"
    )

    # A naked function cannot safely receive the predicate's register use.
    naked_input = work_dir / "naked.ll"
    naked_output = work_dir / "naked-split.ll"
    naked_input.write_text(
        'define void @naked_target() naked {\n'
        'entry:\n'
        '  call void asm sideeffect "", ""()\n'
        '  ret void\n'
        '}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [opt, "-obf-report-skips", "-passes=obf-split,verify", "-S",
         str(naked_input), "-o", str(naked_output)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "reason=naked" in result.stderr, result.stderr
    assert ".obf.split.state" not in naked_output.read_text(encoding="utf-8")

    print(
        "O2 split final executable retains control flow and passes runtime cases "
        f"({len(baseline_code)} -> {len(split_code)} instructions)",
        flush=True,
    )


if __name__ == "__main__":
    main()
