#!/usr/bin/env python3
"""Collect compiler effects across a complete Clang build, then enforce selection."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


PASSES = {
    "obf-string": "sobf", "obf-split": "split", "obf-bcf": "bcf",
    "obf-fla": "fla", "obf-sub": "sub", "obf-const": "constenc",
    "obf-global-access": "gai",
}
LISTS = ("passes", "functions", "string_globals", "access_globals", "constants")


def read_config(path):
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("configuration must be an object with version 1")
    if set(config) - {"version", "strict", "seed", *LISTS}:
        raise ValueError("unknown configuration field")
    if not isinstance(config.get("strict", False), bool):
        raise ValueError("strict must be a boolean")
    for field in LISTS:
        values = config.setdefault(field, [])
        if not isinstance(values, list) or any(
                not isinstance(value, str) or not value or
                any(c in value for c in (",", "\n", "\r", "\0")) for value in values):
            raise ValueError(f"{field} must be an array of nonempty, comma-free strings")
        if len(set(values)) != len(values):
            raise ValueError(f"duplicate {field} selector")
    if not config["passes"] or set(config["passes"]) - PASSES.keys():
        raise ValueError("passes must select registered obf-* passes")
    if config["string_globals"] and "obf-string" not in config["passes"]:
        raise ValueError("string_globals requires obf-string")
    if bool(config["access_globals"]) != ("obf-global-access" in config["passes"]):
        raise ValueError("obf-global-access requires access_globals and vice versa")
    if bool(config["constants"]) != ("obf-const" in config["passes"]):
        raise ValueError("obf-const requires constants and vice versa")
    if config["functions"] and config["passes"] == ["obf-string"]:
        raise ValueError("Clang string selection uses string_globals, not functions")
    if "seed" in config and (not isinstance(config["seed"], str) or
                            not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{32}", config["seed"])):
        raise ValueError("seed must contain 32 hexadecimal digits")
    return config


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def flags(config):
    options = ["-" + short for name, short in PASSES.items() if name in config["passes"]]
    for field, option in (("functions", "obf-only-functions"),
                          ("string_globals", "sobf-only-globals"),
                          ("access_globals", "gai-only-globals"),
                          ("constants", "constenc-values")):
        if config[field]:
            options.append(f"-{option}=" + ",".join(config[field]))
    if config.get("seed"):
        options.append("-obf-test-seed=" + config["seed"])
    return [arg for option in options for arg in ("-mllvm", option)]


def start(config_path, session):
    config = read_config(config_path)
    session.mkdir(parents=True, exist_ok=False)
    write_json(session / "config.json", config)
    (session / "invocations").mkdir()


def compile_one(session, command):
    if not command:
        raise ValueError("compile requires -- followed by the Clang command")
    if (session / "report.json").exists():
        raise ValueError("session is already finished; start a fresh build session")
    config = read_config(session / "config.json")
    identity = uuid.uuid4().hex
    directory = session / "invocations" / identity
    directory.mkdir()
    # A killed launcher leaves this marker, making incomplete builds fail closed.
    pending = directory / "pending"
    pending.touch()
    events = directory / "events"
    events.mkdir()
    actual = [command[0], *flags(config), *command[1:]]
    record = {"id": identity, "directory": os.getcwd(), "command": actual}
    before = time.perf_counter_ns()
    try:
        result = subprocess.run(actual, env={**os.environ, "OBF_EVENT_DIR": str(events)})
        record["exit_code"] = result.returncode
    except OSError as error:
        record.update(exit_code=127, error=str(error))
    record["wall_ns"] = time.perf_counter_ns() - before
    write_json(directory / "result.json", record)
    pending.unlink()
    return record["exit_code"] if record["exit_code"] >= 0 else 128 - record["exit_code"]


def summarize(config, invocations, events, pending):
    summaries = {}
    strict_passed = bool(invocations) and not pending and all(
        item["exit_code"] == 0 for item in invocations)
    for name in config["passes"]:
        short = PASSES[name]
        primary_kind = "global" if short in ("sobf", "gai") else "function"
        related = [event for event in events if event["pass"] == short]
        matching = [event for event in related if event.get("reason") not in
                    ("not-selected", "not-found", "declaration")]
        effects = [event for event in matching if event["event"] == "effect" and
                   event["count"] > 0]
        primary_effects = [event for event in effects if event["kind"] == primary_kind]
        selectors = []
        if short != "sobf":
            selectors += [("function", symbol) for symbol in config["functions"]]
        if short in ("sobf", "gai"):
            field = "string_globals" if short == "sobf" else "access_globals"
            selectors += [("global", symbol) for symbol in config[field]]
        absent, unchanged = [], []
        for kind, symbol in selectors:
            if not any(event["kind"] == kind and event["raw_name"] == symbol
                       for event in matching):
                absent.append({"kind": kind, "raw_name": symbol})
            elif not any(event["kind"] == kind and event["raw_name"] == symbol
                         for event in effects):
                unchanged.append({"kind": kind, "raw_name": symbol})
        # Preserve translation-unit/process identity when counting repeated
        # internal names. Exact selectors are intentionally build-wide.
        def count(items):
            return len({(e["invocation"], e["event_file"], e["kind"], e["raw_name"])
                        for e in items})
        summaries[name] = {
            "matched_symbols": count(e for e in matching if e["kind"] == primary_kind),
            "transformed_symbols": count(primary_effects),
            "transformed_sites": sum(e["count"] for e in primary_effects),
            "skipped_symbols": count(e for e in matching if e["event"] == "skip"),
            "absent_selectors": absent, "untransformed_selectors": unchanged,
        }
        strict_passed &= bool(primary_effects) and not absent and not unchanged
    return {"schema_version": 1, "config": config, "strict_passed": bool(strict_passed),
            "coverage_status": "compiler-events" if events else "no-compiler-events",
            "pending_invocations": pending, "invocations": invocations,
            "pass_summary": summaries, "events": events,
            "limitations": "Compiler events do not prove effects survived linking. "
                           "Selectors are resolved across this build, not per translation unit."}


def finish(session):
    config = read_config(session / "config.json")
    invocations, events, pending = [], [], []
    for directory in sorted((session / "invocations").iterdir()):
        if (directory / "pending").exists() or not (directory / "result.json").is_file():
            pending.append(directory.name)
            continue
        invocations.append(json.loads((directory / "result.json").read_text()))
        for path in sorted((directory / "events").glob("*.jsonl")):
            for line in path.read_text().splitlines():
                event = json.loads(line)
                if (event.get("event") not in ("effect", "skip") or
                        event.get("kind") not in ("function", "global", "block", "module") or
                        not isinstance(event.get("raw_name"), str) or
                        type(event.get("count")) is not int or event["count"] < 0):
                    raise ValueError(f"invalid compiler event: {path}")
                events.append({**event, "invocation": directory.name, "event_file": path.name})
    report = summarize(config, invocations, events, pending)
    write_json(session / "report.json", report)
    print(session / "report.json")
    if pending or any(item["exit_code"] != 0 for item in invocations):
        return 1
    return 2 if config.get("strict", False) and not report["strict_passed"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    first = commands.add_parser("start", help="create a fresh session before building")
    first.add_argument("--config", required=True, type=Path)
    for child in (first, commands.add_parser("compile"), commands.add_parser("finish")):
        child.add_argument("--session", required=True, type=Path)
        if child.prog.endswith("compile"):
            child.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    session = args.session.resolve()
    if args.action == "start":
        start(args.config, session)
        return 0
    if args.action == "compile":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return compile_one(session, command)
    return finish(session)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"clang-obf: {error}", file=sys.stderr)
        sys.exit(1)
