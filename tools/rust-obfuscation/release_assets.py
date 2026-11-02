"""Stage release assets only after all four Rust archives have matching smoke proof."""

import argparse
import json
from pathlib import Path
import shutil

from verify_integrity import sha256, verify_archive_sidecar


PLATFORMS = ("linux-amd64", "linux-arm64", "macos-arm64", "windows-amd64")


def exactly_one(root, name):
    matches = list(root.rglob(name))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {name}, found {len(matches)}")
    return matches[0]


def collect(artifacts, tag, expected_commit):
    assets = []
    for platform in PLATFORMS:
        clang = exactly_one(artifacts / f"archive-{platform}",
                            f"llvm-obfuscator-{tag}-{platform}.zip")
        verify_archive_sidecar(clang)
        assets.append((clang, clang.name))
        assets.append((clang.parent / (clang.name + ".sha256"),
                       clang.name + ".sha256"))
        root = artifacts / f"rust-obfuscation-{platform}"
        extension = "zip" if platform.startswith("windows") else "tar.gz"
        archive = exactly_one(root, f"rust-obfuscation-{platform}.{extension}")
        verify_archive_sidecar(archive)
        smoke = exactly_one(root, "smoke-report.json")
        report = json.loads(smoke.read_text())
        if report.get("source_commit") != expected_commit:
            raise ValueError(f"{platform}: smoke report is for a different source commit")
        if (report.get("source_dirty") is not False or
                any(report.get(field) is not True for field in
                    ("passed", "full", "live_https", "benchmark"))):
            raise ValueError(f"{platform}: full clean extracted-toolchain qualification is missing")
        if report.get("archive_sha256") != sha256(archive):
            raise ValueError(f"{platform}: smoke report tested a different archive")
        benchmark = exactly_one(root, "benchmark-report.json")
        assets.extend(((archive, archive.name),
                       (archive.with_name(archive.name + ".sha256"), archive.name + ".sha256"),
                       (smoke, f"rust-obfuscation-{platform}-smoke.json"),
                       (benchmark, f"rust-obfuscation-{platform}-benchmark.json")))
    return assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assets = collect(args.artifacts, args.tag, args.expected_commit)
    args.output.mkdir(parents=True, exist_ok=False)
    for source, name in assets:
        shutil.copy2(source, args.output / name)
        print(name)


if __name__ == "__main__":
    main()
