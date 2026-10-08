"""Exercise selective global access indirection through opt and Clang."""

import argparse
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from test_transforms import target_body


CASES = ((0, 0), (1, 2), (17, 29), (42, 5),
         (0xFFFFFFFF, 0xFFFFFFFF), (0x6B7D4A91, 0xE32B09A7))
UNSAFE_GLOBALS = (
    "tls_value", "weak_value", "constant_value", "volatile_value",
    "atomic_value", "escaped_value", "section_value", "metadata_value",
    "addrspace_value", "external_init_value", "nonlocal_value", "float_value",
    "personality_value", "special_cc_value", "invariant_group_value",
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


def opt_emit(opt, source, output, names, extra=(), passes="obf-global-access,verify"):
    result = run([
        opt, f"-gai-only-globals={names}", *extra, f"-passes={passes}",
        "-S", str(source), "-o", str(output),
    ])
    verify(opt, output)
    return output.read_text(encoding="utf-8"), result.stderr


def has_slot(ir, name):
    return re.search(rf"@\.obf\.gai[^\s=]*{re.escape(name)}[^\s=]*\s*=",
                     ir) is not None


def same_body_ignoring_metadata_ids(left, right, function):
    # New private globals shift printed metadata numbers without changing an
    # untouched function's instructions or metadata kinds.
    def normalize(body):
        return re.sub(r"(![A-Za-z0-9_.]+) !\d+", r"\1 !<id>", body)

    return normalize(target_body(left, function)) == normalize(
        target_body(right, function)
    )


def assert_indirected(ir, baseline, function, global_name):
    plain = target_body(baseline, function)
    encoded = target_body(ir, function)
    assert f"ptr @{global_name}" in plain, (
        f"{function}: fixture has no direct access to {global_name}"
    )
    assert f"ptr @{global_name}" not in encoded, (
        f"{function}: transformed body retained a direct global access"
    )
    assert has_slot(ir, global_name), (
        f"{function}: selected global has no private pointer slot"
    )
    if re.search(r"load ptr, ptr @\.obf\.gai", encoded):
        return
    helper_call = re.search(
        rf"@(?P<helper>\.obf\.gai\.get\.{re.escape(global_name)}"
        rf"(?:\.\d+)?)\(", encoded
    )
    assert helper_call, f"{function}: no pointer-slot load or helper call"
    helper = target_body(ir, helper_call.group("helper"))
    assert "load ptr" in helper and ".obf.gai" in helper, (
        f"{function}: helper does not load the selected pointer slot"
    )


def check_opt(opt, work_dir):
    source = Path(__file__).with_name("global_access.ll").resolve()
    baseline_path = work_dir / "opt-baseline.ll"
    run([opt, "-passes=verify", "-S", str(source), "-o", str(baseline_path)])
    baseline = baseline_path.read_text(encoding="utf-8")

    no_list = run([opt, "-passes=obf-global-access", "-disable-output",
                   str(source)], check=False)
    assert no_list.returncode != 0 and "gai-only-globals" in no_list.stderr, (
        "enabled global access indirection accepted an empty global allowlist"
    )

    first = work_dir / "opt-selected.ll"
    twice = work_dir / "opt-selected-twice.ll"
    encoded, diagnostics = opt_emit(
        opt, source, first, "selected_value", ("-obf-report-skips",)
    )
    opt_emit(opt, source, twice, "selected_value", (),
             "obf-global-access,obf-global-access,verify")
    assert first.read_bytes() == twice.read_bytes(), (
        "a second obf-global-access pass changed already indirected IR"
    )
    assert_indirected(encoded, baseline, "selected_user", "selected_value")
    assert same_body_ignoring_metadata_ids(encoded, baseline, "peer_user")
    assert not has_slot(encoded, "selected_value_peer"), (
        "exact global allowlist matched a longer peer name"
    )
    assert 'symbol="selected_value_peer" reason=not-selected' in diagnostics

    missing_path = work_dir / "opt-missing.ll"
    _, missing_diag = opt_emit(
        opt, source, missing_path, "missing_value", ("-obf-report-skips",)
    )
    assert 'symbol="missing_value" reason=not-found' in missing_diag

    both_path = work_dir / "opt-both.ll"
    both, _ = opt_emit(opt, source, both_path,
                       "selected_value,selected_value_peer")
    assert_indirected(both, baseline, "selected_user", "selected_value")
    assert_indirected(both, baseline, "peer_user", "selected_value_peer")

    filtered_path = work_dir / "opt-function-filter.ll"
    filtered, filtered_diag = opt_emit(
        opt, source, filtered_path, "selected_value,function_filtered",
        ("-obf-only-functions=selected_user", "-obf-report-skips"),
    )
    assert_indirected(filtered, baseline, "selected_user", "selected_value")
    assert same_body_ignoring_metadata_ids(filtered, baseline, "blocked_user")
    assert not has_slot(filtered, "function_filtered")
    assert 'symbol="function_filtered" reason=function-not-selected' in filtered_diag

    excluded_path = work_dir / "opt-exclusions.ll"
    excluded, excluded_diag = opt_emit(
        opt, source, excluded_path, ",".join(UNSAFE_GLOBALS),
        ("-obf-report-skips",),
    )
    for name in UNSAFE_GLOBALS:
        assert not has_slot(excluded, name), (
            f"unsafe global {name} unexpectedly received a pointer slot"
        )
    for name, reason in (("tls_value", "thread-local"),
                         ("weak_value", "non-local"),
                         ("volatile_value", "unsafe-use"),
                         ("atomic_value", "unsafe-use"),
                         ("escaped_value", "unsafe-use"),
                         ("section_value", "section"),
                         ("personality_value", "function-abi"),
                         ("special_cc_value", "function-abi"),
                         ("invariant_group_value", "invariant-group")):
        assert f'symbol="{name}" reason={reason}' in excluded_diag, (
            f"missing explicit {reason} skip reason for {name}"
        )

    optimized = work_dir / "opt-reoptimized.ll"
    run([opt, "-O2", "-S", str(first), "-o", str(optimized)])
    verify(opt, optimized)
    print("direct opt selection, exclusions, idempotence, and O2 verify passed",
          flush=True)


def clang_emit(clang, opt, source, work_dir, level, name, options):
    stem = work_dir / f"{level}-{name}"
    ir_path = stem.with_suffix(".ll")
    executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
    flags = [piece for option in options for piece in ("-mllvm", option)]
    command = [*clang, f"-{level}", "-fno-discard-value-names", *flags,
               str(source)]
    ir_result = run([*command, "-S", "-emit-llvm", "-o", str(ir_path)])
    verify(opt, ir_path)
    run([*command, "-o", str(executable)])
    outputs = [run([str(executable), str(x), str(y)], timeout=10).stdout
               for x, y in CASES]
    return ir_path.read_text(encoding="utf-8"), executable, outputs, ir_result.stderr


def disassemble_symbol(objdump, object_path, symbol):
    help_result = run([objdump, "--help"], check=False)
    help_text = help_result.stdout + help_result.stderr
    if "--disassemble-symbols=" in help_text:
        selector = f"--disassemble-symbols={symbol}"
    elif "--disassemble=" in help_text:
        selector = f"--disassemble={symbol}"
    else:
        return None
    result = run([objdump, "--no-show-raw-insn", "--reloc", selector,
                  str(object_path)], check=False)
    if result.returncode or symbol not in result.stdout:
        return None
    return result.stdout.lower()


def check_elf_slot_relocation(objdump, object_path, helper, global_name):
    # ELF assemblers can relocate a local slot through its section symbol.
    # Follow the helper's slot-section relocation, then check that the slot's
    # relocation resolves to the selected global's section and exact offset.
    slot_match = re.search(
        r"\br_[a-z0-9_]+\s+(\.data\.rel\.ro(?:\.local)?)"
        r"(?:[+-]0x[0-9a-f]+)?\b", helper,
    )
    assert slot_match, "ELF helper does not address a read-only pointer slot"
    slot_section = slot_match.group(1)
    relocations = run([objdump, "-r", str(object_path)]).stdout.lower()
    section_match = re.search(
        rf"(?ms)^relocation records for \[{re.escape(slot_section)}\]:\n"
        r"(.*?)(?=^relocation records for \[|\Z)", relocations,
    )
    assert section_match, "ELF pointer-slot relocation section is missing"
    target_match = re.search(
        r"(?m)^0+\s+r_[a-z0-9_]+\s+(\S+)", section_match.group(1)
    )
    assert target_match, "ELF pointer slot has no relocation at offset zero"
    target = target_match.group(1)
    if target == global_name:
        return

    symbols = run([objdump, "-t", str(object_path)]).stdout.lower()
    symbol = None
    for line in symbols.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[-1] == global_name:
            symbol = fields
            break
    assert symbol, f"ELF symbol table has no {global_name}"
    symbol_offset = int(symbol[0], 16)
    symbol_section = symbol[-3]
    if target == symbol_section:
        relocation_offset = 0
    elif target.startswith(symbol_section + "+0x"):
        relocation_offset = int(target[len(symbol_section) + 1:], 16)
    else:
        raise AssertionError(
            f"ELF pointer slot targets {target}, not {global_name}'s section"
        )
    assert relocation_offset == symbol_offset, (
        f"ELF pointer slot targets offset {relocation_offset:#x}, "
        f"not {global_name} at {symbol_offset:#x}"
    )


def check_object_pattern(clang, source, work_dir, objdump, required):
    if not objdump:
        print("object disassembly skipped: no llvm-objdump/objdump found", flush=True)
        return
    symbol = "_selected" if sys.platform == "darwin" else "selected"
    objects = {}
    for name, flags in {
        "baseline": (),
        "selected": ("-gai", "-gai-only-globals=selected_value",
                     "-obf-only-functions=selected"),
    }.items():
        path = work_dir / f"O2-{name}.o"
        options = [piece for flag in flags for piece in ("-mllvm", flag)]
        run([*clang, "-O2", *options, "-c", str(source), "-o", str(path)])
        objects[name] = path
    plain = disassemble_symbol(objdump, objects["baseline"], symbol)
    encoded = disassemble_symbol(objdump, objects["selected"], symbol)
    if plain is None or encoded is None:
        if required:
            raise AssertionError("--objdump could not disassemble selected() objects")
        print("object disassembly skipped: selected symbol unavailable", flush=True)
        return
    assert ".obf.gai" not in plain
    helper_symbol = ("_" if sys.platform == "darwin" else "") + (
        ".obf.gai.get.selected_value"
    )
    if helper_symbol in encoded:
        helper = disassemble_symbol(objdump, objects["selected"], helper_symbol)
        assert helper is not None, "selected() calls a missing object helper"
        if "file format coff-" in helper:
            # COFF resolves a private slot to its .rdata section symbol. The
            # section's relocation in turn identifies the selected global.
            relocations = run([objdump, "-r", str(objects["selected"])]).stdout.lower()
            assert re.search(r"image_rel_amd64_rel32\s+\.rdata\b", helper), (
                "COFF helper does not address its pointer-slot section"
            )
            assert re.search(
                r"relocation records for \[\.rdata\]:[\s\S]*?"
                r"image_rel_amd64_addr64\s+selected_value\b", relocations
            ), "COFF pointer-slot section does not point to selected_value"
        elif "file format elf" in helper:
            direct_slot = re.search(
                r"\br_[a-z0-9_]+\s+\.obf\.gai\.selected_value"
                r"(?:[+-]0x[0-9a-f]+)?\b", helper,
            )
            if not direct_slot:
                check_elf_slot_relocation(objdump, objects["selected"], helper,
                                          "selected_value")
        else:
            assert ".obf.gai.selected_value" in helper, (
                "selected() calls a helper, but its object lacks a slot relocation"
            )
        assert re.search(r"\b(?:ldr|movq|mov)\b", helper), (
            "global access helper object lacks a pointer-slot load"
        )
    elif ".obf.gai.selected_value" not in encoded:
        if required:
            raise AssertionError(
                "selected() object disassembly lacks a pointer-slot relocation"
            )
        print("object slot relocation unavailable in disassembly", flush=True)
        return
    print("selected() final object references its pointer slot", flush=True)


def check_lto(clang, source, work_dir, baseline_outputs, objdump, required,
              require_lto):
    binaries = {}
    for name, flags in {
        "baseline": (),
        "selected": ("-gai", "-gai-only-globals=selected_value",
                     "-obf-only-functions=selected"),
    }.items():
        path = work_dir / f"lto-{name}"
        options = [piece for flag in flags for piece in ("-mllvm", flag)]
        result = run([*clang, "-O2", "-flto", *options, str(source),
                      "-o", str(path)], timeout=180, check=False)
        if result.returncode:
            if name == "baseline":
                if require_lto:
                    raise AssertionError(
                        "baseline LTO link is required but failed:\n"
                        f"{result.stderr}"
                    )
                print("LTO runtime skipped: baseline linker does not support this "
                      "compiler's bitcode", flush=True)
                return
            raise AssertionError(
                "selected LTO link failed after baseline succeeded:\n"
                f"{result.stderr}"
            )
        binaries[name] = path
    outputs = [run([str(binaries["selected"]), str(x), str(y)], timeout=10).stdout
               for x, y in CASES]
    assert outputs == baseline_outputs, "LTO changed selected runtime behavior"
    print("LTO baseline and selected runtime results match", flush=True)

    if not objdump:
        print("LTO disassembly skipped: no objdump found", flush=True)
        return
    symbol = "_selected" if sys.platform == "darwin" else "selected"
    plain = disassemble_symbol(objdump, binaries["baseline"], symbol)
    encoded = disassemble_symbol(objdump, binaries["selected"], symbol)
    if plain is None or encoded is None:
        if required:
            raise AssertionError("--objdump could not disassemble LTO selected()")
        print("LTO disassembly skipped: selected symbol unavailable", flush=True)
        return

    # A link-time optimizer can fold an immutable pointer slot back into a
    # direct address even though the prelink IR and runtime remain correct.
    if "file format coff-" not in plain:
        assert re.search(r"<_?selected_value>", plain), (
            "LTO baseline disassembly lacks a visible direct global reference"
        )
        assert not re.search(r"<_?selected_value>", encoded), (
            "LTO folded the selected pointer slot into a direct global reference"
        )
    helper_symbol = ("_" if sys.platform == "darwin" else "") + (
        ".obf.gai.get.selected_value"
    )
    assert helper_symbol in encoded, (
        "LTO selected() does not call the pointer-slot helper"
    )
    helper = disassemble_symbol(objdump, binaries["selected"], helper_symbol)
    assert helper is not None, "LTO pointer-slot helper is missing from final binary"
    assert not re.search(r"<_?selected_value>", helper), (
        "LTO helper accesses the selected global directly"
    )
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        assert re.search(r"\bldr\s+x\d+,\s*\[", helper), (
            "LTO helper lacks an AArch64 pointer-slot load"
        )
    elif machine in ("x86_64", "amd64"):
        assert re.search(r"\bmov[q]?\b[^\n]*(?:\([^\n]*\)|\[[^\n]*\])", helper), (
            "LTO helper lacks an x86_64 pointer-slot load"
        )
    elif required:
        raise AssertionError(f"LTO slot-load check does not support {machine}")
    print("LTO final binary retains the selected pointer-slot load", flush=True)


def check_clang(clang, opt, work_dir, objdump, required, require_lto):
    source = Path(__file__).with_name("global_access.c").resolve()
    variants = {
        "baseline": (),
        "selected": ("-gai", "-gai-only-globals=selected_value",
                     "-obf-only-functions=selected", "-obf-report-skips"),
        "both": ("-gai", "-gai-only-globals=selected_value,selected_value_peer"),
        "veto": ("-gai", "-gai-only-globals=vetoed_value",
                 "-obf-report-skips"),
        "annotation": ("-gai-only-globals=annotated_value",
                       "-obf-only-functions=annotated"),
    }
    for level in ("O0", "O2"):
        emitted = {}
        binaries = {}
        outputs = {}
        diagnostics = {}
        for name, flags in variants.items():
            ir, executable, result, stderr = clang_emit(
                clang, opt, source, work_dir, level, name, flags
            )
            emitted[name] = ir
            binaries[name] = executable
            outputs[name] = result
            diagnostics[name] = stderr
            assert result == outputs["baseline"], (
                f"{level}/{name} changed program output"
            )
            print(f"[{level}] {name} global access compiled and ran", flush=True)

        baseline = emitted["baseline"]
        selected = emitted["selected"]
        assert_indirected(selected, baseline, "selected", "selected_value")
        for function in ("peer", "vetoed", "annotated"):
            assert same_body_ignoring_metadata_ids(selected, baseline, function), (
                f"{level}/selected changed {function}"
            )
        assert 'symbol="selected_value_peer" reason=not-selected' in diagnostics["selected"]

        both = emitted["both"]
        assert_indirected(both, baseline, "selected", "selected_value")
        assert_indirected(both, baseline, "peer", "selected_value_peer")
        veto = emitted["veto"]
        assert same_body_ignoring_metadata_ids(veto, baseline, "vetoed")
        assert not has_slot(veto, "vetoed_value")
        assert "reason=negative-annotation" in diagnostics["veto"]
        annotation = emitted["annotation"]
        assert_indirected(annotation, baseline, "annotated", "annotated_value")
        for function in ("selected", "peer", "vetoed"):
            assert same_body_ignoring_metadata_ids(annotation, baseline, function), (
                f"{level}/annotation changed {function}"
            )
        print(f"[{level}] exact global selection and negative annotation passed",
              flush=True)

        if level == "O2":
            check_object_pattern(clang, source, work_dir, objdump, required)
            check_lto(clang, source, work_dir, outputs["baseline"],
                      objdump, required, require_lto)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--objdump", type=Path)
    parser.add_argument("--require-lto", action="store_true")
    args = parser.parse_args()
    if args.require_lto and not args.objdump:
        parser.error("--require-lto requires --objdump for final binary verification")

    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    clang = [str(args.clang.resolve())]
    if args.sysroot:
        clang.extend(("-isysroot", str(args.sysroot.resolve())))
    elif sys.platform == "darwin":
        clang.extend(("-isysroot", run(["xcrun", "--show-sdk-path"]).stdout.strip()))
    opt = str(args.opt.resolve())
    objdump = (str(args.objdump.resolve()) if args.objdump else
               shutil.which("llvm-objdump") or shutil.which("objdump"))

    check_opt(opt, work_dir)
    check_clang(clang, opt, work_dir, objdump, args.objdump is not None,
                args.require_lto)


if __name__ == "__main__":
    main()
