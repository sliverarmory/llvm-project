"""Read one linked or object-file function without including adjacent code."""

from functools import lru_cache
from pathlib import Path
import re
import subprocess


def _dump(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(
            f"objdump exited {result.returncode}: {command!r}\n{result.stderr}")
    return result.stdout


@lru_cache(maxsize=8)
def _coff_unwind_ranges(objdump, binary, mtime_ns, size):
    """COFF exports have no sizes; PE unwind entries give exact function ends."""
    dump = _dump([str(objdump), "--private-headers", "--unwind-info", str(binary)])
    image_base = re.search(r"(?m)^ImageBase\s+([0-9a-fA-F]+)$", dump)
    if image_base is None:
        raise RuntimeError(f"PE image base absent in {binary}")
    base = int(image_base.group(1), 16)
    ranges = re.findall(
        r"(?m)^Function Table:\s*\n\s+Start Address: 0x([0-9a-fA-F]+)"
        r"\s*\n\s+End Address: 0x([0-9a-fA-F]+)", dump)
    # A PE containing only leaf functions may have no unwind table at all.
    return {base + int(start, 16): base + int(end, 16)
            for start, end in ranges}


def _coff_leaf_range(entries, raw, start):
    """Allow only straight-line leaves whose terminating instruction is clear.

    Export-only PE disassembly labels private tail-call targets as raw+offset,
    so a branch target cannot prove where the exported function ends. Fixtures
    retain frame pointers to get exact unwind metadata for branching probes.
    """
    for index, (address, line) in enumerate(entries):
        opcode = line.split(None, 1)[0]
        if opcode.startswith("j") or opcode.startswith("loop"):
            raise RuntimeError(f"branching PE function {raw} needs an unwind range")
        if opcode in ("ret", "retq", "ud2"):
            return entries[:index + 1]
    raise RuntimeError(f"could not bound PE leaf function {raw}")


def isolated_instructions(objdump, path, raw, required=True):
    """Return raw assembly lines for exactly ``raw``, or [] if optional/absent.

    ELF, Mach-O, and COFF objects retain ordinary symbol ranges. Linked PE
    exports can instead make objdump print all following .text, so they need
    unwind bounds or a straight-line leaf fallback. File magic, rather than the
    Python host, distinguishes linked PE from saved COFF objects.
    """
    path = Path(path)
    dump = _dump([str(objdump), f"--disassemble-symbols={raw}",
                  "--no-show-raw-insn", str(path)])
    label = re.search(rf"(?m)^\s*([0-9a-fA-F]+) <{re.escape(raw)}>:\s*$", dump)
    if label is None:
        if required:
            raise RuntimeError(f"function {raw} absent in {path}")
        return []
    start = int(label.group(1), 16)
    with path.open("rb") as binary:
        linked_pe = binary.read(2) == b"MZ"
    body = dump[label.end():]
    if not linked_pe:
        for boundary in re.finditer(
                r"(?m)^\s*([0-9a-fA-F]+) <[^>]+>:\s*$", body):
            # An export and a retained symbol can both label the same address.
            if int(boundary.group(1), 16) != start:
                body = body[:boundary.start()]
                break
    entries = [(int(address, 16), line.strip()) for address, line in re.findall(
        r"(?m)^\s*([0-9a-fA-F]+):\s+(.+)$", body)]
    if not entries:
        raise RuntimeError(f"no instructions for {raw} in {path}")
    if linked_pe:
        stat = path.stat()
        end = _coff_unwind_ranges(objdump, path, stat.st_mtime_ns,
                                  stat.st_size).get(start)
        entries = ([(address, line) for address, line in entries
                    if start <= address < end] if end is not None else
                   _coff_leaf_range(entries, raw, start))
        if not entries:
            raise RuntimeError(f"empty PE function range for {raw} in {path}")
    return [line for _, line in entries]
