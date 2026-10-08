"""Exercise selective integer constant encoding through opt and Clang.

The IR checks use exact typed allowlists. The executable checks compare each
transformed program with an untransformed build at both O0 and O2.
"""

import argparse
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from test_transforms import target_body


SEED_A = "00112233445566778899aabbccddeeff"
SEED_B = "ffeeddccbbaa99887766554433221100"
IR_VALUES = (
    "i8:-83,i16:-26307,i32:1803373201,"
    "i64:7339196651810267521,i32:1"
)
C_VALUES = "i32:1803373201,i32:3811248551"
CASES = ((0, 0), (1, 2), (17, 29), (42, 5),
         (0xFFFFFFFF, 0xFFFFFFFF), (0x6B7D4A91, 0xE32B09A7))
FUNCTIONS = ("selected", "neighbor", "annotated", "vetoed")
ORIGINAL_OPERATIONS = {
    "encode_i8": "add i8 %x, -83",
    "encode_i16": "xor i16 %x, -26307",
    "encode_i32": "add nsw i32 %x, 1803373201",
    "encode_i64": "icmp ult i64 %x, 7339196651810267521",
    "encode_one": "add i32 %x, 1",
}
DIRECT_C_SITES = (
    re.compile(r"\badd i32 [^\n,]+, 1803373201(?:\s|,|$)"),
    re.compile(r"\bicmp ult i32 [^\n,]+, -483718745(?:\s|,|$)"),
)


def run(command, timeout=120, check=True):
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"Timed out after {timeout}s: {command!r}") from error
    if check and result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def verify(opt, path):
    run([opt, "-passes=verify", "-disable-output", str(path)])


def opt_emit(opt, source, output, values, seed, passes="obf-const,verify"):
    run([
        opt, f"-obf-test-seed={seed}", f"-constenc-values={values}",
        f"-passes={passes}", "-S", str(source), "-o", str(output),
    ])
    verify(opt, output)
    return output.read_text(encoding="utf-8")


def check_opt_pass(opt, work_dir):
    source = Path(__file__).with_name("constant_encoding.ll").resolve()
    # LLVM 23 canonicalizes a repeated vector constant to `splat` while
    # printing IR. Compare against the same round trip, without our pass.
    baseline_path = work_dir / "opt-baseline.ll"
    run([opt, "-passes=verify", "-S", str(source), "-o", str(baseline_path)])
    baseline = baseline_path.read_text(encoding="utf-8")
    first = work_dir / "opt-first.ll"
    repeated = work_dir / "opt-repeated.ll"
    different = work_dir / "opt-different.ll"
    twice = work_dir / "opt-twice.ll"

    encoded = opt_emit(opt, source, first, IR_VALUES, SEED_A)
    opt_emit(opt, source, repeated, IR_VALUES, SEED_A)
    opt_emit(opt, source, different, IR_VALUES, SEED_B)
    opt_emit(opt, source, twice, IR_VALUES, SEED_A,
             "obf-const,obf-const,verify")
    assert first.read_bytes() == repeated.read_bytes(), (
        "the same seed changed constant encoding IR"
    )
    assert first.read_bytes() != different.read_bytes(), (
        "different seeds produced identical constant encoding IR"
    )
    assert first.read_bytes() == twice.read_bytes(), (
        "a second obf-const pass encoded generated instructions or changed IR"
    )

    for name, operation in ORIGINAL_OPERATIONS.items():
        plain_body = target_body(baseline, name)
        encoded_body = target_body(encoded, name)
        assert operation in plain_body, f"fixture lacks {name}'s selected operation"
        assert operation not in encoded_body, f"{name} retained its selected literal"
        assert "!obf.constenc" in encoded_body, f"{name} lacks encoding metadata"
    for name in ("unselected_value", "unsupported_vector", "caller_undef",
                 "caller_poison"):
        assert target_body(encoded, name) == target_body(baseline, name), (
            f"{name} changed despite not having an eligible selected scalar operand"
        )

    only_i32 = work_dir / "opt-only-i32.ll"
    filtered = opt_emit(opt, source, only_i32, "i32:1803373201", SEED_A)
    assert target_body(filtered, "encode_i32") != target_body(baseline, "encode_i32")
    for name in ("encode_i8", "encode_i16", "encode_i64", "encode_one",
                 "unselected_value", "unsupported_vector"):
        assert target_body(filtered, name) == target_body(baseline, name), (
            f"typed i32 allowlist unexpectedly changed {name}"
        )

    signed = work_dir / "opt-signed-i8.ll"
    unsigned = work_dir / "opt-unsigned-i8.ll"
    signed_ir = opt_emit(opt, source, signed, "i8:-83", SEED_A)
    unsigned_ir = opt_emit(opt, source, unsigned, "i8:173", SEED_A)
    assert target_body(signed_ir, "encode_i8") == target_body(unsigned_ir, "encode_i8"), (
        "signed and unsigned aliases selected different i8 constants"
    )
    for name in ("encode_i16", "encode_i32", "encode_i64", "encode_one"):
        assert target_body(signed_ir, name) == target_body(baseline, name), (
            f"i8-only allowlist changed {name}"
        )

    optimized = work_dir / "opt-reoptimized.ll"
    run([opt, "-O2", "-S", str(first), "-o", str(optimized)])
    verify(opt, optimized)
    print("direct opt typed selection, seeds, idempotence, and O2 verification passed",
          flush=True)


def direct_c_sites(body):
    return sum(bool(pattern.search(body)) for pattern in DIRECT_C_SITES)


def clang_emit(clang, opt, source, work_dir, level, name, options):
    stem = work_dir / f"{level}-{name}"
    ir_path = stem.with_suffix(".ll")
    executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
    flags = [piece for option in options for piece in ("-mllvm", option)]
    common = [*clang, f"-{level}", "-fno-discard-value-names", *flags,
              str(source)]
    ir_result = run([*common, "-S", "-emit-llvm", "-o", str(ir_path)])
    verify(opt, ir_path)
    run([*common, "-o", str(executable)])
    outputs = [run([str(executable), str(x), str(y)], timeout=10).stdout
               for x, y in CASES]
    return ir_path.read_text(encoding="utf-8"), executable, outputs, ir_result.stderr


def disassemble_symbol(executable, symbol, objdump):
    tool = str(objdump) if objdump else (
        shutil.which("llvm-objdump") or shutil.which("objdump")
    )
    if not tool:
        return None
    help_result = run([tool, "--help"], check=False)
    help_text = help_result.stdout + help_result.stderr
    if "--disassemble-symbols=" in help_text:
        option = f"--disassemble-symbols={symbol}"
    elif "--disassemble=" in help_text:
        option = f"--disassemble={symbol}"
    else:
        return None
    result = run([tool, "--no-show-raw-insn", option, str(executable)],
                 check=False)
    if result.returncode or symbol not in result.stdout:
        return None
    return result.stdout.lower()


def check_binary_pattern(baseline, encoded, objdump, artifact="final binary"):
    machine = platform.machine().lower()
    if machine not in ("x86_64", "amd64", "arm64", "aarch64"):
        if objdump:
            raise AssertionError(f"instruction-immediate check does not support {machine}")
        print("instruction-immediate check skipped: unsupported host architecture",
              flush=True)
        return
    symbol = "_selected" if sys.platform == "darwin" else "selected"
    baseline_asm = disassemble_symbol(baseline, symbol, objdump)
    encoded_asm = disassemble_symbol(encoded, symbol, objdump)
    if baseline_asm is None or encoded_asm is None:
        if objdump:
            raise AssertionError(
                f"--objdump could not disassemble selected() in both {artifact} inputs"
            )
        print("binary immediate check skipped: symbol disassembly unavailable", flush=True)
        return
    if machine in ("arm64", "aarch64"):
        # AArch64 materializes the selected 32-bit literal in two 16-bit
        # halves. The encoded function should instead load its share rows.
        markers = ("0x4a91", "0x6b7d")
        visible = all(marker in baseline_asm for marker in markers)
    else:
        # x86_64 can expose the full literal as one instruction immediate.
        markers = tuple(marker for marker in ("0x6b7d4a91", "0xe32b09a7")
                        if marker in baseline_asm)
        visible = bool(markers)
    if not visible:
        if objdump:
            raise AssertionError("--objdump did not expose the expected baseline immediate")
        print("binary immediate check skipped: baseline immediates not visible",
              flush=True)
        return
    for marker in markers:
        assert marker not in encoded_asm, (
            f"selected() retained {marker} as a {artifact} instruction immediate"
        )
    if machine in ("arm64", "aarch64"):
        assert len(re.findall(r"\bldr\b", encoded_asm)) >= 2, (
            "selected() did not load both encoded share values in final arm64 code"
        )
    print(f"selected() {artifact} has no selected visible immediates", flush=True)


def check_windows_object_pattern(clang, source, work_dir, objdump, variants):
    # The MSVC linker can omit the COFF symbol table from a linked PE. Check
    # the named selected() function in COFF objects instead; executable
    # behavior is still compared against the baseline for every input above.
    objects = {}
    for name in ("baseline", "selected"):
        flags = [piece for option in (*variants[name], f"-obf-test-seed={SEED_A}")
                 for piece in ("-mllvm", option)]
        output = work_dir / f"O2-{name}.obj"
        run([*clang, "-O2", *flags, "-c", str(source), "-o", str(output)])
        objects[name] = output
    check_binary_pattern(objects["baseline"], objects["selected"], objdump,
                         artifact="COFF object")


def check_clang(clang, opt, work_dir, objdump):
    source = Path(__file__).with_name("constant_encoding.c").resolve()
    variants = {
        "baseline": ("-obf-only-functions=__none__",),
        "selected": ("-constenc", "-obf-only-functions=selected",
                     f"-constenc-values={C_VALUES}", "-obf-report-skips"),
        "annotation": ("-obf-only-functions=annotated",
                       f"-constenc-values={C_VALUES}"),
        "veto": ("-constenc", "-obf-only-functions=vetoed",
                 f"-constenc-values={C_VALUES}", "-obf-report-skips"),
        "cap-zero": ("-constenc", "-obf-only-functions=selected",
                     f"-constenc-values={C_VALUES}", "-constenc-max-sites=0"),
        "cap-one": ("-constenc", "-obf-only-functions=selected",
                    f"-constenc-values={C_VALUES}", "-constenc-max-sites=1"),
    }
    for level in ("O0", "O2"):
        emitted = {}
        binaries = {}
        outputs = {}
        diagnostics = {}
        for name, options in variants.items():
            options = (*options, f"-obf-test-seed={SEED_A}")
            ir, exe, result, stderr = clang_emit(
                clang, opt, source, work_dir, level, name, options
            )
            emitted[name] = ir
            binaries[name] = exe
            outputs[name] = result
            diagnostics[name] = stderr
            assert result == outputs["baseline"], (
                f"{level}/{name} changed program output for a differential case"
            )
            print(f"[{level}] {name} constant encoding compiled and ran", flush=True)

        plain = emitted["baseline"]
        for name in FUNCTIONS:
            assert direct_c_sites(target_body(plain, name)) == 2, (
                f"{level}/{name} baseline lacks both selected literal sites"
            )

        selected = emitted["selected"]
        assert direct_c_sites(target_body(selected, "selected")) == 0
        assert "!obf.constenc" in target_body(selected, "selected")
        for name in ("neighbor", "annotated", "vetoed"):
            assert target_body(selected, name) == target_body(plain, name), (
                f"{level}/selected changed unselected function {name}"
            )
        assert 'symbol="neighbor" reason=not-selected' in diagnostics["selected"]

        annotated = emitted["annotation"]
        assert direct_c_sites(target_body(annotated, "annotated")) == 0
        for name in ("selected", "neighbor", "vetoed"):
            assert target_body(annotated, name) == target_body(plain, name), (
                f"{level}/annotation changed unselected function {name}"
            )

        veto = emitted["veto"]
        assert target_body(veto, "vetoed") == target_body(plain, "vetoed")
        assert 'symbol="vetoed" reason=negative-annotation' in diagnostics["veto"]

        zero = emitted["cap-zero"]
        one = emitted["cap-one"]
        assert target_body(zero, "selected") == target_body(plain, "selected")
        assert direct_c_sites(target_body(one, "selected")) == 1, (
            f"{level}: max-sites=1 did not encode exactly one selected site"
        )
        assert target_body(one, "neighbor") == target_body(plain, "neighbor")
        if level == "O2":
            if sys.platform == "win32":
                check_windows_object_pattern(clang, source, work_dir, objdump,
                                             variants)
            else:
                check_binary_pattern(binaries["baseline"], binaries["selected"],
                                     objdump)
        print(f"[{level}] selection, annotation, veto, and site caps passed",
              flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--objdump", type=Path)
    args = parser.parse_args()

    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    clang = [str(args.clang.resolve())]
    if args.sysroot:
        clang.extend(("-isysroot", str(args.sysroot.resolve())))
    elif sys.platform == "darwin":
        clang.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).stdout.strip()))
    opt = str(args.opt.resolve())
    objdump = args.objdump.resolve() if args.objdump else None
    if objdump and not objdump.is_file():
        parser.error(f"--objdump does not exist: {objdump}")
    check_opt_pass(opt, work_dir)
    check_clang(clang, opt, work_dir, objdump)


if __name__ == "__main__":
    main()
