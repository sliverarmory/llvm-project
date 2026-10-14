#!/usr/bin/env python3
"""Exercise every Rust obfuscation pass beside unwind cleanup.

The pinned custom Rust 1.99 stage1 compiler builds each pass separately and
the supported seven-pass pipeline at O0/O2 with panic=abort/unwind. The test
checks emitted IR, linked code, recorded pass effects or exact safety skips,
and runtime results including Drop order and checked-overflow unwinding.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from machine_code import isolated_instructions


SOURCE = Path(__file__).with_name("eh_regions.rs")
SEED = "00112233445566778899aabbccddeeff"
STRING_WITNESS = "explicit unwind witness"
PASSES = (
    "obf-string", "obf-split", "obf-bcf", "obf-fla", "obf-sub",
    "obf-const", "obf-global-access",
)
EVENT_NAMES = {
    "obf-string": "sobf", "obf-split": "split", "obf-bcf": "bcf",
    "obf-fla": "fla", "obf-sub": "sub", "obf-const": "constenc",
    "obf-global-access": "gai",
}
VARIANTS = {"plain": (), **{name: (name,) for name in PASSES},
            "combined": PASSES}
EH_OPCODES = ("landingpad", "cleanuppad", "catchpad", "catchswitch",
              "catchret", "cleanupret", "resume", "invoke")
EXPECTED_UNWIND = (
    "6:0:29:2:33:3\n"
    "7:0:46:2:33:5\n"
    "6:1:panic:explicit unwind witness:2:33:3\n"
    "2147483647:2:panic:2:33:5\n"
)
EXPECTED_ABORT = "6:0:29:2:33:3\n7:0:46:2:33:5\n"


def run(argv: list[str], *, check: bool = True, timeout: int = 600,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True,
                            timeout=timeout, env=env)
    if check and result.returncode:
        raise AssertionError(
            f"command exited {result.returncode}: {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def function_body(ir: str) -> str:
    match = re.search(r"(?m)^define\b[^\n]*@eh_probe\([^\n]*\{\n", ir)
    if not match:
        raise AssertionError("emitted IR lacks eh_probe definition")
    end = ir.find("\n}", match.end())
    if end < 0:
        raise AssertionError("eh_probe definition is incomplete")
    return ir[match.end():end]


def unwind_targets(body: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\binvoke\b[^\n]*\n\s*to label %\S+ "
                            r"unwind label %(\S+)", body))


def opcode_count(body: str, opcode: str) -> int:
    return len(re.findall(rf"(?m)^\s+(?:%[^\s=]+\s*=\s*)?{opcode}\b",
                          body))


def instruction_count(body: str) -> int:
    return len(re.findall(r"(?m)^\s+%[^=\n]+\s=\s", body))


def linked_instructions(objdump: Path, binary: Path) -> tuple[str, ...]:
    symbol = "_eh_probe" if sys.platform == "darwin" else "eh_probe"
    # Addresses and branch targets can move when another variant adds code.
    # An opcode difference proves the selected EH function changed after link.
    return tuple(line.split(None, 1)[0]
                 for line in isolated_instructions(objdump, binary, symbol))


def selected_globals(ir: str, body: str) -> tuple[str, str, str]:
    literal = re.findall(
        rf'(?m)^@([^ =]+) = [^\n]* constant \[23 x i8\] '
        rf'c"{re.escape(STRING_WITNESS)}"', ir)
    if len(literal) != 1:
        raise AssertionError("baseline must contain one selected Rust byte literal")
    scalars = re.findall(r"(?m)^@([^ =]*EH_STATE(?:\.\d+)?) = ([^\n]+)", ir)
    if len(scalars) != 1:
        raise AssertionError("baseline must contain one private EH_STATE scalar")
    scalar, declaration = scalars[0]
    if not declaration.startswith("internal ") or scalar not in body:
        raise AssertionError("EH_STATE must be private and accessed by eh_probe")
    if re.search(r"\bglobal i32\b", declaration):
        scalar_type = "i32"
    elif "global [4 x i8]" in declaration:
        # Rust's O0 representation is a byte array, which this pass rejects.
        scalar_type = "byte-array"
    else:
        raise AssertionError(f"unexpected EH_STATE representation: {declaration}")
    return literal[0], scalar, scalar_type


def check_eh(label: str, ir: str, baseline_ir: str, body: str,
             baseline: str, panic: str) -> None:
    if panic == "unwind":
        expected_pad = "cleanuppad" if sys.platform == "win32" else "landingpad"
        if opcode_count(baseline, expected_pad) == 0 or not unwind_targets(baseline):
            raise AssertionError(f"{label}: baseline misses unwind EH")
        for opcode in EH_OPCODES:
            if opcode_count(body, opcode) != opcode_count(baseline, opcode):
                raise AssertionError(f"{label}: {opcode} count changed")
        if unwind_targets(body) != unwind_targets(baseline):
            raise AssertionError(f"{label}: invoke unwind destinations changed")
        if sys.platform == "win32":
            # catch_unwind also creates a native MSVC catch region outside
            # eh_probe. Check the whole module so it cannot disappear while
            # the probe's cleanup pads happen to remain intact.
            for opcode in ("catchswitch", "catchpad"):
                expected = opcode_count(baseline_ir, opcode)
                if expected == 0:
                    raise AssertionError(f"{label}: Windows baseline lacks {opcode}")
                if opcode_count(ir, opcode) != expected:
                    raise AssertionError(f"{label}: module {opcode} count changed")
    elif any(opcode_count(body, opcode) for opcode in EH_OPCODES):
        raise AssertionError(f"{label}: panic=abort unexpectedly gained EH")


def expected_outcome(pass_name: str, panic: str,
                     scalar_type: str) -> tuple[str, str | None]:
    if pass_name == "obf-const" and panic == "unwind":
        return "skip", "unsupported-control-flow"
    if pass_name == "obf-global-access":
        if scalar_type == "byte-array":
            return "skip", "not-integer"
        if panic == "unwind":
            return "skip", "function-abi"
    return "effect", None


def check_events(label: str, event_file: Path, passes: tuple[str, ...],
                 literal: str, scalar: str, scalar_type: str,
                 panic: str, body: str) -> dict[str, str]:
    if not event_file.is_file():
        raise AssertionError(f"{label}: compiler emitted no pass event file")
    records = [json.loads(line) for line in event_file.read_text(
        encoding="utf-8").splitlines()]
    outcomes = {}
    for pass_name in passes:
        kind = "global" if pass_name in ("obf-string", "obf-global-access") \
            else "function"
        symbol = (literal if pass_name == "obf-string" else
                  scalar if pass_name == "obf-global-access" else "eh_probe")
        selected = [record for record in records
                    if record["pass"] == EVENT_NAMES[pass_name]
                    and record["kind"] == kind
                    and record["raw_name"] == symbol]
        expectation, reason = expected_outcome(pass_name, panic, scalar_type)
        effects = [record for record in selected if record["event"] == "effect"
                   and record["count"] > 0]
        skips = [record["reason"] for record in selected
                 if record["event"] == "skip"]
        if expectation == "effect":
            if not effects:
                raise AssertionError(
                    f"{label}: {pass_name} had no selected effect; skips={skips}")
        elif effects or not skips or set(skips) != {reason}:
            raise AssertionError(
                f"{label}: {pass_name} expected skip {reason}, "
                f"got effects={len(effects)} skips={skips}")
        if pass_name in ("obf-split", "obf-bcf", "obf-fla"):
            protected = [record["raw_name"] for record in records
                         if record["pass"] == EVENT_NAMES[pass_name]
                         and record["kind"] == "block"
                         and record["event"] == "skip"
                         and record["reason"] == "protected-eh-region"]
            if panic == "unwind":
                pad = "cleanuppad" if sys.platform == "win32" else "landingpad"
                if (len(protected) < opcode_count(body, pad)
                        or any(not name for name in protected)
                        or len(set(protected)) != len(protected)):
                    raise AssertionError(
                        f"{label}: {pass_name} omitted or duplicated protected "
                        f"EH block skips: {protected}")
            elif protected:
                raise AssertionError(
                    f"{label}: {pass_name} reported protected EH blocks "
                    f"under panic=abort: {protected}")
        outcomes[pass_name] = expectation if reason is None else f"skip:{reason}"
    return outcomes


def check_effect_ir(label: str, ir: str, binary: Path, body: str,
                    baseline: str, passes: tuple[str, ...],
                    outcomes: dict[str, str], scalar: str, panic: str) -> None:
    if "obf-string" in passes:
        if (STRING_WITNESS in ir or STRING_WITNESS.encode() in binary.read_bytes()
                or "@llvm.global_ctors" not in ir or ".datadiv_decode" not in ir):
            raise AssertionError(f"{label}: selected Rust byte literal was not encoded")
    if "obf-split" in passes:
        if (".obf.split.state" not in ir or ".split" not in body or
                opcode_count(body, "br") <= opcode_count(baseline, "br")):
            raise AssertionError(f"{label}: split did not add control flow")
    if "obf-bcf" in passes:
        if ("@.obf.bcf.x" not in ir or "urem i32" not in body or
                opcode_count(body, "br") <= opcode_count(baseline, "br")):
            raise AssertionError(f"{label}: BCF predicate missing")
    if "obf-fla" in passes:
        if "switch i32" not in body:
            raise AssertionError(f"{label}: flattening dispatcher missing")
        if panic == "unwind":
            if "eh.dispatch" not in body or "store volatile i32" not in body:
                raise AssertionError(f"{label}: EH normal-region dispatcher missing")
        elif "switchVar" not in body or "loopEntry" not in body:
            raise AssertionError(f"{label}: full-function dispatcher missing")
    if "obf-sub" in passes:
        if "sub.freeze" not in body or instruction_count(body) <= instruction_count(baseline):
            raise AssertionError(f"{label}: operator substitution did not expand IR")
    if "obf-const" in passes:
        if outcomes["obf-const"] == "effect":
            if (".obf.const.a" not in ir or ".obf.const.b" not in ir or
                    "obf.const.decoded" not in body):
                raise AssertionError(f"{label}: selected constant not encoded")
        elif "obf.const.decoded" in body:
            raise AssertionError(f"{label}: protected EH function gained constant encoding")
    if "obf-global-access" in passes:
        helper = f".obf.gai.get.{scalar}"
        if outcomes["obf-global-access"] == "effect":
            if helper not in ir or "obf.gai.ptr" not in body:
                raise AssertionError(f"{label}: private scalar access not indirected")
        elif helper in ir or "obf.gai.ptr" in body:
            raise AssertionError(f"{label}: skipped global access was transformed")


def compile_case(rustc: Path, opt: Path, objdump: Path, work_dir: Path, level: int,
                 panic: str, variant: str, baseline: str | None,
                 baseline_ir: str | None, witnesses: tuple[str, str, str] | None,
                 baseline_machine: tuple[str, ...] | None
                 ) -> tuple[str, str, tuple[str, str, str], tuple[str, ...]]:
    label = f"O{level}-{panic}-{variant}"
    stem = work_dir / label
    ir_path = stem.with_suffix(".ll")
    binary = stem.with_suffix(".exe") if sys.platform == "win32" else stem
    event_file = stem.with_suffix(".jsonl")
    event_file.unlink(missing_ok=True)
    args = [
        str(rustc), "--edition=2024", "-C", f"opt-level={level}",
        "-C", f"panic={panic}", "-C", "overflow-checks=yes",
        "-C", "codegen-units=1", "-C", "lto=off",
    ]
    if sys.platform == "win32":
        # PE linkers may omit the symbol table needed for disassembly. Export
        # the same probe from every baseline and selected executable.
        args.extend(("-C", "link-arg=/EXPORT:eh_probe",
                     "-C", "force-frame-pointers=yes"))
    passes = VARIANTS[variant]
    env = None
    if passes:
        assert witnesses is not None
        literal, scalar, _ = witnesses
        llvm_args = [
            f"-rust-obf-pipeline={'all' if variant == 'combined' else passes[0]}",
            "-obf-only-functions=eh_probe", f"-obf-test-seed={SEED}",
            f"-sobf-only-globals={literal}", f"-gai-only-globals={scalar}",
            "-constenc-values=i32:11", "-bcf_prob=100", "-split_num=2",
            "-obf-report-skips",
        ]
        args.extend(("-C", f"llvm-args={' '.join(llvm_args)}"))
        env = os.environ.copy()
        env["RUST_OBF_EVENT_FILE"] = str(event_file)

    # Keep the selected private-global names identical in the emitted IR and
    # executable: separate rustc invocations can anonymize them differently.
    run([*args, f"--emit=llvm-ir={ir_path},link={binary}", str(SOURCE)], env=env)
    ir = ir_path.read_text(encoding="utf-8")
    body = function_body(ir)
    if baseline is None:
        witnesses = selected_globals(ir, body)
        check_eh(label, ir, ir, body, body, panic)
    else:
        assert witnesses is not None and baseline_ir is not None
        assert baseline_machine is not None
        check_eh(label, ir, baseline_ir, body, baseline, panic)
    run([str(opt), "-passes=verify", "-disable-output", str(ir_path)])

    outcomes = {}
    if passes:
        literal, scalar, scalar_type = witnesses
        outcomes = check_events(label, event_file, passes, literal, scalar,
                                scalar_type, panic, body)
        check_effect_ir(label, ir, binary, body, baseline, passes, outcomes,
                        scalar, panic)
    actual = run([str(binary)], timeout=30).stdout
    expected = EXPECTED_UNWIND if panic == "unwind" else EXPECTED_ABORT
    if actual != expected:
        raise AssertionError(
            f"{label}: runtime mismatch:\n{actual}\nexpected:\n{expected}"
        )
    if panic == "abort":
        failed = run([str(binary), "--uncaught-panic"], check=False,
                     timeout=30)
        if failed.returncode == 0:
            raise AssertionError(f"{label}: uncaught panic did not abort")
    machine = linked_instructions(objdump, binary)
    if baseline_machine is not None and any(
            name != "obf-string" and outcome == "effect"
            for name, outcome in outcomes.items()):
        if machine == baseline_machine:
            raise AssertionError(
                f"{label}: selected EH pass changed IR but not linked eh_probe code")
    summary = ", ".join(f"{name}={outcome}" for name, outcome in outcomes.items())
    print(f"PASS {label}: IR, linked code, EH, Drop/panic/overflow runtime"
          f"{'; ' + summary if summary else ''}", flush=True)
    return ir, body, witnesses, machine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--objdump", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    rustc = args.rustc.resolve()
    opt = args.opt.resolve()
    objdump = args.objdump.resolve()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    version = run([str(rustc), "-vV"], timeout=30).stdout
    if not re.search(r"^release: 1\.99\.", version, re.MULTILINE) or not re.search(
            r"^LLVM version: 23\.", version, re.MULTILINE):
        raise AssertionError("expected pinned custom Rust 1.99 / LLVM 23")

    for level in (0, 2):
        for panic in ("unwind", "abort"):
            baseline_ir, baseline, witnesses, baseline_machine = compile_case(
                rustc, opt, objdump, work_dir, level, panic, "plain",
                None, None, None, None)
            for variant in VARIANTS:
                if variant != "plain":
                    compile_case(rustc, opt, objdump, work_dir, level, panic,
                                 variant, baseline, baseline_ir, witnesses,
                                 baseline_machine)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
