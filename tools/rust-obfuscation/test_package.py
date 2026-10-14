"""Archive boundary and integrity regressions for the delivered toolchain."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import bootstrap
from package import PINS, ignored_sources, manifest_files, validated_rust_source_commit
from provenance import (STAGE_RECORD, digest_json, file_inventory, sha256,
                        build_wrapper,
                        source_identity, stage_inventory, verify_stage,
                        verify_wrapper, wrapper_record_path, write_record)
from verify_integrity import (validate_native_rustc_host,
                              verify_archive_sidecar, verify_tree)
from verify_package import extract, verify_llvm_tool_version


class PackageIntegrityTests(unittest.TestCase):
    def test_llvm_version_allows_runtime_host_changes_only(self):
        recorded = ("LLVM (http://llvm.org/):\n"
                    "  LLVM version 23.1.3\n"
                    "  Optimized build with assertions.\n"
                    "  Default target: arm64-apple-darwin24.6.0\n"
                    "  Host CPU: (unknown)")
        actual = recorded.replace("darwin24.6.0", "darwin27.0.0").replace(
            "(unknown)", "apple-m5")
        verify_llvm_tool_version(actual, recorded, "opt")
        for changed in (actual.replace("23.1.3", "23.1.4"),
                        actual.replace("Optimized build", "DEBUG build"),
                        actual.replace(" with assertions", "")):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "extracted opt version differs"):
                    verify_llvm_tool_version(changed, recorded, "opt")

    def test_llvm_version_preserves_registered_target_identity(self):
        recorded = ("LLVM (http://llvm.org/):\n"
                    "  LLVM version 23.1.3\n"
                    "  Optimized build with assertions.\n\n\n"
                    "  Registered Targets:\n"
                    "    aarch64 - AArch64 (little endian)\n"
                    "    x86 - 32-bit X86: Pentium-Pro and above")
        verify_llvm_tool_version(recorded, recorded, "llvm-objdump")
        with self.assertRaisesRegex(ValueError, "extracted llvm-objdump version differs"):
            verify_llvm_tool_version(recorded.replace("    x86 -", "    arm -"),
                                     recorded, "llvm-objdump")

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
            inventory = {"archive_sha256": PINS["rust_source_sha256"], "files":
                         file_inventory([source / "bootstrap.example.toml", source / "src/version"], source)}
            write_record(source / bootstrap.RUST_INVENTORY, inventory)
            with mock.patch.dict(bootstrap.PINS, rust_source_inventory_sha256=digest_json(inventory)):
                self.assertEqual(validated_rust_source_commit(source), PINS["rust_commit"])
            marker.write_text("wrong archive checksum\n")
            with self.assertRaisesRegex(RuntimeError, "unverified Rust source tree"):
                validated_rust_source_commit(source)

    def test_archive_tree_rejects_changed_missing_extra_and_forged_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / "compiler").mkdir()
            code = source / "compiler/input.rs"
            code.write_text("original")
            inventory = {"archive_sha256": PINS["rust_source_sha256"],
                         "files": file_inventory([code], source)}
            write_record(source / bootstrap.RUST_INVENTORY, inventory)
            with mock.patch.dict(bootstrap.PINS, rust_source_inventory_sha256=digest_json(inventory)):
                bootstrap.verify_rust_archive_tree(source)
                (source / "build").mkdir()
                (source / "build/generated.rs").write_text("generated")
                (source / "bootstrap-obfuscation.toml").write_text("generated")
                bootstrap.verify_rust_archive_tree(source)
                code.write_text("changed")
                with self.assertRaisesRegex(RuntimeError, "changed=.*compiler/input.rs"):
                    bootstrap.verify_rust_archive_tree(source)
                code.unlink()
                with self.assertRaisesRegex(RuntimeError, "missing=.*compiler/input.rs"):
                    bootstrap.verify_rust_archive_tree(source)
                code.write_text("original")
                extra = source / "compiler/extra.rs"
                extra.write_text("extra input")
                with self.assertRaisesRegex(RuntimeError, "extra=.*compiler/extra.rs"):
                    bootstrap.verify_rust_archive_tree(source)
                forged = {**inventory, "files": file_inventory([code, extra], source)}
                write_record(source / bootstrap.RUST_INVENTORY, forged)
                with self.assertRaisesRegex(RuntimeError, "pinned archive inventory"):
                    bootstrap.verify_rust_archive_tree(source)

    def test_source_identity_survives_commit_but_detects_source_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            code = root / "input.cpp"
            code.write_text("first")
            identity = source_identity(root, ("input.cpp",))
            subprocess.run(["git", "-C", str(root), "add", "input.cpp"], check=True)
            self.assertEqual(identity, source_identity(root, ("input.cpp",)))
            subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false",
                            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "-qm", "inputs"], check=True)
            self.assertEqual(identity, source_identity(root, ("input.cpp",)))
            code.write_text("changed")
            self.assertNotEqual(identity, source_identity(root, ("input.cpp",)))
            code.unlink()
            self.assertNotEqual(identity, source_identity(root, ("input.cpp",)))

    def test_stage_provenance_rejects_stale_source_binary_and_link_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage = root / "stage1"
            (stage / "bin").mkdir(parents=True)
            rustc = stage / "bin/rustc"
            rustc.write_bytes(b"compiler")
            library = root / "LLVMObfuscation.a"
            library.write_bytes(b"archive")
            tool = root / "opt"
            tool.write_bytes(b"opt")
            record = {"schema_version": 1, "kind": "rust-stage1", "source_identity": "current",
                      "outputs": stage_inventory(stage), "llvm_tools": file_inventory([tool], root),
                      "llvm_inputs": file_inventory([library])}
            write_record(stage / STAGE_RECORD, record)
            with mock.patch("provenance.source_identity", return_value="current"):
                verify_stage(stage, [tool], root)
                rustc.write_bytes(b"different compiler")
                with self.assertRaisesRegex(ValueError, "compiler/sysroot"):
                    verify_stage(stage, [tool], root)
                rustc.write_bytes(b"compiler")
                library.write_bytes(b"new archive")
                with self.assertRaisesRegex(ValueError, "inputs or outputs changed"):
                    verify_stage(stage, [tool], root)
            with mock.patch("provenance.source_identity", return_value="new source"):
                with self.assertRaisesRegex(ValueError, "LLVM/build source changed"):
                    verify_stage(stage, [tool], root)

    def test_stage_inventory_prunes_sources_before_traversal_and_matches_package(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp) / "stage1"
            expected_names = ("bin/rustc", "bin/rustdoc", "lib/librustc_driver.so",
                              "lib/rustlib/native/lib/libtest.rlib",
                              "lib/rustlib/native/lib/libstd.rlib", "share/src/keep.rs")
            expected_paths = []
            for name in (*expected_names, "lib/rustlib/src/rust/omitted.rs",
                         "lib/rustlib/rustc-src/rust/omitted.rs", STAGE_RECORD):
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
                if name in expected_names:
                    expected_paths.append(path)
            expected = file_inventory(expected_paths, stage)
            excluded = [stage / "lib/rustlib" / name for name in ("src", "rustc-src")]
            scandir = os.scandir

            def guarded_scandir(directory):
                self.assertFalse(any(Path(directory).is_relative_to(path) for path in excluded),
                                 f"traversed excluded Rust sources: {directory}")
                return scandir(directory)

            with mock.patch("provenance.os.scandir", side_effect=guarded_scandir):
                self.assertEqual(stage_inventory(stage), expected)
            delivered = Path(temp) / "delivered"
            shutil.copytree(stage, delivered, ignore=ignored_sources)
            for name in ("src", "rustc-src"):
                self.assertFalse((delivered / "lib/rustlib" / name).exists())
            self.assertEqual(stage_inventory(delivered), expected)

    @unittest.skipIf(os.name == "nt", "Windows source junctions are tested separately")
    def test_stage_inventory_hashes_file_links_without_following_directory_links(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp) / "stage1"
            (stage / "bin").mkdir(parents=True)
            rustc = stage / "bin/rustc"
            rustc.write_bytes(b"compiler")
            alias = stage / "bin/rustc-alias"
            alias.symlink_to("rustc")
            (stage / "cycle").symlink_to(stage, target_is_directory=True)
            (stage / "dangling").symlink_to("missing")
            self.assertEqual(stage_inventory(stage), file_inventory([rustc, alias], stage))

    @unittest.skipUnless(os.name == "nt", "requires a native Windows directory junction")
    def test_stage_inventory_prunes_cyclic_windows_source_junctions(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "rust"
            stage = source / "build/stage1"
            rustc = stage / "bin/rustc.exe"
            rustc.parent.mkdir(parents=True)
            rustc.write_bytes(b"compiler")
            junctions = []
            try:
                for name in ("src", "rustc-src"):
                    junction = stage / "lib/rustlib" / name / "rust"
                    junction.parent.mkdir(parents=True)
                    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                                    str(junction), str(source)], check=True,
                                   capture_output=True, text=True, timeout=10)
                    junctions.append(junction)
                    self.assertTrue(junction.is_junction())
                # Keep a traversal regression bounded even with the real cycle:
                # source/build/stage1/lib/rustlib/src/rust -> source.
                result = subprocess.run(
                    [sys.executable, "-B", "-c",
                     "import json, sys; from pathlib import Path; "
                     "from provenance import stage_inventory; "
                     "print(json.dumps(stage_inventory(Path(sys.argv[1]))))", str(stage)],
                    cwd=Path(__file__).parent, check=True, capture_output=True,
                    text=True, timeout=10)
                self.assertEqual(json.loads(result.stdout), file_inventory([rustc], stage))
            finally:
                # Remove only each junction before TemporaryDirectory cleanup.
                for junction in reversed(junctions):
                    os.rmdir(junction)

    def test_wrapper_provenance_rejects_stale_source_and_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            wrapper = Path(temp) / "rust-obf-cargo"
            wrapper.write_bytes(b"wrapper")
            write_record(wrapper_record_path(wrapper), {"schema_version": 1, "kind": "cargo-wrapper",
                "source_identity": "old", "binary_sha256": sha256(wrapper)})
            with mock.patch("provenance.source_identity", return_value="new"):
                with self.assertRaisesRegex(ValueError, "wrapper source changed"):
                    verify_wrapper(wrapper)
            wrapper.write_bytes(b"changed")
            with mock.patch("provenance.source_identity", return_value="old"):
                with self.assertRaisesRegex(ValueError, "wrapper binary differs"):
                    verify_wrapper(wrapper)

    def test_wrapper_uses_cargo_artifact_under_inherited_build_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            canonical = target / "release" / ("rust-obf-cargo.exe" if os.name == "nt" else "rust-obf-cargo")
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(b"STALE WRAPPER")
            artifact = target / "native-host" / "release" / canonical.name
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"FRESH WRAPPER")
            compiler = root / "sysroot" / "bin" / ("rustc.exe" if os.name == "nt" else "rustc")
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"compiler")
            cargo = root / "cargo"
            cargo.write_bytes(b"cargo")

            def fake_command(command, **kwargs):
                if command[1:] == ["--print", "sysroot"]:
                    output = str(compiler.parent.parent)
                elif command[1:] == ["-vV"]:
                    output = "rustc 1.99.0\nhost: native-host\n"
                elif command[1] == "build":
                    self.assertEqual(command[command.index("--target") + 1], "native-host")
                    self.assertEqual(kwargs["env"]["RUSTC"], str(compiler))
                    output = json.dumps({"reason": "compiler-artifact", "target": {
                        "name": "rust-obf-cargo", "kind": ["bin"]}, "executable": str(artifact)})
                else:
                    output = "cargo 1.99.0"
                return subprocess.CompletedProcess(command, 0, stdout=output)

            with mock.patch.dict(os.environ, CARGO_BUILD_TARGET="unrelated-target"), \
                 mock.patch("provenance.source_identity", return_value="current"), \
                 mock.patch("provenance.validate_native_rustc_host", return_value="native-host"), \
                 mock.patch("provenance.git", return_value=b"revision"), \
                 mock.patch("provenance.subprocess.run", side_effect=fake_command):
                build_wrapper(target, str(cargo), root)
                self.assertEqual(canonical.read_bytes(), b"FRESH WRAPPER")
                verify_wrapper(canonical, root)

    def test_wrapper_cache_changes_when_compiler_driver_changes_at_same_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            name = "rust-obf-cargo.exe" if os.name == "nt" else "rust-obf-cargo"
            compiler = root / "sysroot" / "bin" / ("rustc.exe" if os.name == "nt" else "rustc")
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"unchanged compiler launcher")
            driver = root / "sysroot" / "lib" / "librustc_driver-test"
            driver.parent.mkdir()
            driver.write_bytes(b"first compiler driver")
            cargo = root / "cargo"
            cargo.write_bytes(b"cargo")
            build_directories = []

            def fake_command(command, **kwargs):
                if command[1:] == ["--print", "sysroot"]:
                    output = str(compiler.parent.parent)
                elif command[1:] == ["-vV"]:
                    output = "rustc 1.99.0\nhost: native-host\n"
                elif command[1] == "build":
                    # Model Cargo's real reuse behavior: the compiler's -vV and
                    # package source are unchanged, so an existing artifact is
                    # considered fresh even if a shared compiler driver changed.
                    directory = Path(kwargs["env"]["CARGO_TARGET_DIR"])
                    build_directories.append(directory)
                    self.assertEqual(command[command.index("--target-dir") + 1], str(directory))
                    artifact = directory / "native-host" / "release" / name
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    if not artifact.exists():
                        artifact.write_bytes(driver.read_bytes())
                    output = json.dumps({"reason": "compiler-artifact", "target": {
                        "name": "rust-obf-cargo", "kind": ["bin"]}, "executable": str(artifact)})
                else:
                    output = "cargo 1.99.0"
                return subprocess.CompletedProcess(command, 0, stdout=output)

            with mock.patch("provenance.source_identity", return_value="current"), \
                 mock.patch("provenance.validate_native_rustc_host", return_value="native-host"), \
                 mock.patch("provenance.git", return_value=b"revision"), \
                 mock.patch("provenance.subprocess.run", side_effect=fake_command):
                build_wrapper(target, str(cargo), root)
                canonical = target / "release" / name
                self.assertEqual(canonical.read_bytes(), b"first compiler driver")
                driver.write_bytes(b"second compiler driver")
                build_wrapper(target, str(cargo), root)
                self.assertEqual(canonical.read_bytes(), b"second compiler driver")
                self.assertNotEqual(build_directories[0], build_directories[1])
                build_wrapper(target, str(cargo), root)
                self.assertEqual(build_directories[1], build_directories[2])
                verify_wrapper(canonical, root)

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
