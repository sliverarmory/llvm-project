import json
from pathlib import Path
import tempfile
import unittest

from release_assets import collect, PLATFORMS
from verify_integrity import sha256


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.smokes = []
        for platform in PLATFORMS:
            clang = self.root / f"archive-{platform}"
            clang.mkdir()
            clang_zip = clang / f"llvm-obfuscator-v1-{platform}.zip"
            clang_zip.write_bytes(b"clang")
            clang_zip.with_name(clang_zip.name + ".sha256").write_text(
                f"{sha256(clang_zip)}  {clang_zip.name}\n")
            rust = self.root / f"rust-obfuscation-{platform}"
            rust.mkdir()
            ext = "zip" if platform.startswith("windows") else "tar.gz"
            archive = rust / f"rust-obfuscation-{platform}.{ext}"
            archive.write_bytes(platform.encode())
            digest = sha256(archive)
            archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n")
            report = {"source_commit": "abc", "source_dirty": False,
                      "archive_sha256": digest, "passed": True, "full": True,
                      "live_https": True, "benchmark": True}
            smoke = rust / "smoke-report.json"
            smoke.write_text(json.dumps(report))
            (rust / "benchmark-report.json").write_text("{}")
            self.smokes.append(smoke)

    def test_stages_all_four_hosts_and_unique_evidence_names(self):
        assets = collect(self.root, "v1", "abc")
        self.assertEqual(24, len(assets))
        self.assertEqual(24, len({name for _, name in assets}))

    def test_rejects_wrong_revision_or_incomplete_smoke(self):
        for field, value in (("source_commit", "old"), ("source_dirty", True),
                             ("passed", False), ("full", False),
                             ("live_https", False), ("benchmark", False),
                             ("archive_sha256", "wrong")):
            with self.subTest(field=field):
                path = self.smokes[0]
                old = path.read_text()
                report = json.loads(old)
                report[field] = value
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    collect(self.root, "v1", "abc")
                path.write_text(old)

    def test_rejects_missing_host(self):
        self.smokes[-1].unlink()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            collect(self.root, "v1", "abc")

    def test_rejects_archive_changed_since_smoke(self):
        archive = next((self.root / "rust-obfuscation-linux-amd64").glob("*.tar.gz"))
        archive.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            collect(self.root, "v1", "abc")


if __name__ == "__main__":
    unittest.main()
