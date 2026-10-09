"""Archive boundary and integrity regressions for the delivered toolchain."""

import hashlib
import json
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from package import PINS, manifest_files, validated_rust_source_commit
from verify_integrity import (validate_native_rustc_host,
                              verify_archive_sidecar, verify_tree)
from verify_package import extract


class PackageIntegrityTests(unittest.TestCase):
    def test_verified_rust_tarball_does_not_inherit_parent_git_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            subprocess.run(["git", "init", "-q", str(parent)], check=True,
                           capture_output=True, text=True)
            subprocess.run([
                "git", "-C", str(parent), "-c", "commit.gpgsign=false",
                "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "--allow-empty", "-m", "LLVM parent",
            ], check=True, capture_output=True, text=True)
            source = parent / "build-rust-1.99"
            (source / "src").mkdir(parents=True)
            (source / "bootstrap.example.toml").write_text("")
            (source / "src" / "version").write_text(PINS["rust_version"] + "\n")
            marker = source / ".rust-source-sha256"
            marker.write_text(PINS["rust_source_sha256"] + "\n")
            self.assertEqual(validated_rust_source_commit(source), PINS["rust_commit"])
            marker.write_text("wrong archive checksum\n")
            with self.assertRaisesRegex(RuntimeError, "unverified Rust source tree"):
                validated_rust_source_commit(source)

    def test_rejects_zip_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("package/../../escape", "unexpected")
            with self.assertRaisesRegex(ValueError, "unsafe archive entry"):
                extract(archive, root / "out")
            self.assertFalse((root / "escape").exists())

    def test_rejects_windows_aliases_before_extraction(self):
        for name in ("package/file:stream", "package/NUL.txt", "package/com1",
                     "package/dir. /file", "package/name ", "package/a?b"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as out:
                    out.writestr(name, "unexpected")
                with self.assertRaisesRegex(ValueError, "unsafe archive entry"):
                    extract(archive, root / "out")

    def test_rejects_tar_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as out:
                link = tarfile.TarInfo("package/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../escape"
                out.addfile(link)
            with self.assertRaisesRegex(ValueError, "unsupported entry"):
                extract(archive, root / "out")

    def test_rejects_zip_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            link = zipfile.ZipInfo("package/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr(link, "../../escape")
            with self.assertRaisesRegex(ValueError, "unsupported entry"):
                extract(archive, root / "out")

    def test_detects_changed_package_file(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)
            file = package / "payload"
            file.write_bytes(b"original")
            (package / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "source_dirty": False,
                "llvm_source_commit": "example",
                "files": [{"path": "payload", "bytes": 8,
                           "sha256": hashlib.sha256(b"original").hexdigest()}],
            }))
            verify_tree(package)
            file.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_tree(package)

    def test_rejects_unsafe_manifest_path(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)
            (package / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "source_dirty": False,
                "llvm_source_commit": "example",
                "files": [{"path": "bin/CON.exe", "bytes": 0,
                           "sha256": hashlib.sha256(b"").hexdigest()}],
            }))
            with self.assertRaisesRegex(ValueError, "unsafe or duplicate manifest path"):
                verify_tree(package)

    def test_native_rustc_host_must_match_delivery_platform(self):
        hosts = {
            ("Darwin", "arm64"): "aarch64-apple-darwin",
            ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
            ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
            ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
        }
        for (system, machine), triple in hosts.items():
            with self.subTest(system=system, machine=machine):
                self.assertEqual(validate_native_rustc_host(
                    f"release: 1.99.0-dev\nhost: {triple}\n",
                    system=system, machine=machine), triple)
                with self.assertRaisesRegex(ValueError, "does not match native"):
                    validate_native_rustc_host(
                        "host: x86_64-pc-windows-gnu\n",
                        system=system, machine=machine)
        with self.assertRaisesRegex(ValueError, "unsupported native delivery host"):
            validate_native_rustc_host("host: unknown\n", system="Darwin",
                                       machine="x86_64")

    def test_nested_manifest_is_included_in_file_list(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)
            (package / "nested").mkdir()
            (package / "nested" / "manifest.json").write_text("nested")
            self.assertEqual([entry["path"] for entry in manifest_files(package)],
                             ["nested/manifest.json"])

    def test_sidecar_binds_archive_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "toolchain.zip"
            archive.write_bytes(b"archive")
            sidecar = Path(str(archive) + ".sha256")
            digest = hashlib.sha256(b"archive").hexdigest()
            sidecar.write_text(f"{digest}  wrong.zip\n")
            with self.assertRaisesRegex(ValueError, "sidecar filename"):
                verify_archive_sidecar(archive)
            sidecar.write_text(f"{digest}  {archive.name}\n")
            verify_archive_sidecar(archive)


if __name__ == "__main__":
    unittest.main()
