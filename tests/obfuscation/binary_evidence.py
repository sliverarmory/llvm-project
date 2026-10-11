"""Helpers for evidence taken from final native executables."""

import re
import subprocess
import sys
from pathlib import Path

# Reuse the existing PE unwind-bounded function reader: export-only PE output
# can otherwise include neighboring private functions in a probe's evidence.
sys.path.insert(0, str(Path(__file__).with_name("rust")))
from machine_code import isolated_instructions


def checked(command, timeout=30):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command!r}\n{result.stderr}")
    return result.stdout


def sibling_tool(clang, name):
    return Path(clang).resolve().parent / (name + (".exe" if sys.platform == "win32" else ""))


def native_flags(sysroot=None):
    if sysroot is None and sys.platform == "darwin":
        sysroot = checked(["xcrun", "--show-sdk-path"]).strip()
    return ["-isysroot", str(sysroot)] if sysroot else []


def final_instructions(objdump, executable, symbol):
    for spelling in (symbol, "_" + symbol):
        instructions = isolated_instructions(objdump, executable, spelling, required=False)
        if instructions:
            return instructions
    raise RuntimeError(f"final function {symbol} is missing from {executable}")


def instruction_metrics(instructions):
    mnemonics = [line.split(None, 1)[0] for line in instructions]
    branches = sum(
        op.startswith("b.") or op in ("cbz", "cbnz", "tbz", "tbnz")
        or (op.startswith("j") and op not in ("jmp", "jmpq"))
        for op in mnemonics
    )
    calls = sum(op in ("bl", "blr", "call", "callq") for op in mnemonics)
    return {"instruction_count": len(instructions), "conditional_branches": branches,
            "calls": calls, "mnemonics": mnemonics}


def final_section_sizes(objdump, executable):
    output = checked([str(objdump), "--section-headers", str(executable)])
    sections = []
    for line in output.splitlines():
        # ELF includes an unnamed null section and named sections with no
        # Type value. Keep named untyped rows without consuming the next row.
        # Horizontal whitespace is deliberate: a table field cannot cross a
        # line boundary even when the trailing Type column is empty.
        row = re.fullmatch(
            r"[ \t]*\d+[ \t]+(\S+)[ \t]+([0-9a-fA-F]+)[ \t]+"
            r"[0-9a-fA-F]+(?:[ \t]+(.*))?", line)
        if row:
            name, size, kind = row.groups()
            sections.append({"name": name, "bytes": int(size, 16),
                             "kind": (kind or "").strip()})
    if not sections:
        raise RuntimeError(f"no final section sizes found for {executable}")
    return {"sections": sections,
            "text_bytes": sum(row["bytes"] for row in sections if "TEXT" in row["kind"]),
            "initialized_data_bytes": sum(row["bytes"] for row in sections if "DATA" in row["kind"]),
            "bss_bytes": sum(row["bytes"] for row in sections if "BSS" in row["kind"])}


def constant_share_witnesses(ir, binary):
    """Locate compiler-emitted share bytes in the final file (not recovery)."""
    rows = []
    pattern = r'(?m)^@([\w.]*obf\.const\.[ab][\w.]*) = .*?\[2 x i(8|16|32|64)\] \[i\d+ (-?\d+), i\d+ (-?\d+)\]'
    for name, width, first, second in re.findall(pattern, ir):
        width = int(width)
        mask = (1 << width) - 1
        data = b"".join((int(value) & mask).to_bytes(width // 8, "little")
                        for value in (first, second))
        offset = binary.find(data)
        rows.append({"global": name, "width": width, "bytes_hex": data.hex(),
                     "final_file_offset": offset if offset >= 0 else None})
    return rows


def readonly_data_sections(objdump, executable):
    """Extract initialized read-only data from objdump, across ELF/Mach-O/PE."""
    output = checked([str(objdump), "--full-contents", str(executable)])
    sections = []
    current = None
    for line in output.splitlines():
        heading = re.match(r"Contents of section (.+):$", line)
        if heading:
            name = heading.group(1)
            selected = (name.startswith((".rodata", ".rdata")) or
                        name.endswith(",__const"))
            current = {"name": name, "address": None, "data": bytearray()} if selected else None
            if current is not None:
                sections.append(current)
            continue
        if current is None:
            continue
        # Hex columns are followed by at least two spaces before the ASCII
        # rendering. Splitting there prevents ASCII that looks like hex from
        # becoming part of the recovered bytes.
        match = re.match(r"^\s*([0-9a-fA-F]+)\s+(.+)$", line)
        if not match:
            continue
        address = int(match.group(1), 16)
        hex_columns = re.split(r"\s{2,}", match.group(2), maxsplit=1)[0]
        words = hex_columns.split()
        if not words or any(not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,4}", word) for word in words):
            continue
        data = bytes.fromhex("".join(words))
        if current["address"] is None:
            current["address"] = address
        expected_address = current["address"] + len(current["data"])
        if address != expected_address:
            raise RuntimeError(f"noncontiguous data section {current['name']}")
        current["data"].extend(data)
    return sections
