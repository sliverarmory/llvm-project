"""Run the portable obfuscation regression suites with isolated work directories."""

import argparse
import subprocess
import sys
from pathlib import Path


SUITES = (
    ("test_transforms.py", True, False),
    ("test_opt_passes.py", True, False),
    ("test_convergence.py", True, False),
    ("test_cfg_convergence.py", True, False),
    ("test_growth.py", True, False),
    ("test_options.py", False, False),
    ("test_rng.py", True, False),
    ("test_selection.py", True, False),
    ("test_objc_metadata.py", True, False),
    ("test_constant_encoding.py", True, True),
    ("test_global_access.py", True, True),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--objdump", type=Path)
    parser.add_argument("--require-lto", action="store_true",
                        help="require the global-access LTO link and binary check")
    args = parser.parse_args()
    if args.require_lto and not args.objdump:
        parser.error("--require-lto requires --objdump")

    clang = args.clang.resolve()
    opt = args.opt.resolve()
    work_dir = args.work_dir.resolve()
    objdump = args.objdump.resolve() if args.objdump else None
    clangxx = clang.with_name("clang++.exe" if sys.platform == "win32" else "clang++")
    for label, path in (("clang", clang), ("clang++", clangxx), ("opt", opt)):
        if not path.is_file():
            parser.error(f"{label} executable does not exist: {path}")
    if objdump and not objdump.is_file():
        parser.error(f"llvm-objdump executable does not exist: {objdump}")

    resource = subprocess.run(
        [str(clang), "-print-resource-dir"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    if not resource or not Path(resource).is_dir():
        parser.error(f"clang resource directory does not exist: {resource!r}")

    work_dir.mkdir(parents=True, exist_ok=True)
    suite_dir = Path(__file__).resolve().parent
    for script, needs_opt, accepts_objdump in SUITES:
        suite_work_dir = work_dir / Path(script).stem
        command = [
            sys.executable, str(suite_dir / script),
            "--clang", str(clang), "--work-dir", str(suite_work_dir),
        ]
        if needs_opt:
            command.extend(("--opt", str(opt)))
        if accepts_objdump and objdump:
            command.extend(("--objdump", str(objdump)))
        if script == "test_global_access.py" and args.require_lto:
            command.append("--require-lto")
        print(f"running {script}", flush=True)
        subprocess.run(command, check=True)
    print("all portable obfuscation suites passed", flush=True)


if __name__ == "__main__":
    main()
