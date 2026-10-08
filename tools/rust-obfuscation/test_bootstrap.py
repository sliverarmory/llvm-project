"""Regression tests for Rust bootstrap preflight failures seen in milestone 0."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location("rust_obfuscation_bootstrap", Path(__file__).with_name("bootstrap.py"))
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.libdir = self.root / "lib"
        self.libdir.mkdir()
        self.llvm_config = self.root / "bin/llvm-config"
        self.absent = [self.libdir / "libLLVMMCA.a", self.libdir / "libLLVMX86TargetMCA.a"]
        for name in ("libLLVMPasses.a", "libLLVMObfuscation.a"):
            (self.libdir / name).write_bytes(b"archive")

    def fake_llvm_config(self, *args, **_kwargs) -> str:
        if "--libdir" in args:
            return str(self.libdir)
        if "--obj-root" in args:
            return str(self.root)
        if "--libfiles" in args:
            missing = [path for path in self.absent if not path.exists()]
            if missing:
                lines = [f"llvm-config: error: missing: {path}" for path in missing]
                raise RuntimeError("llvm-config failed (1): " + "\n".join(lines))
            return " ".join(str(path) for path in [*self.absent,
                self.libdir / "libLLVMPasses.a", self.libdir / "libLLVMObfuscation.a"])
        if "--libs" in args:
            return "-lLLVMMCA -lLLVMX86TargetMCA -lLLVMPasses -lLLVMObfuscation -lzstd"
        raise AssertionError(f"unexpected llvm-config query: {args}")

    def test_missing_archive_diagnostic_builds_all_targets_then_requeries(self) -> None:
        calls = []

        def fake_ninja(command, **_kwargs):
            calls.append(command)
            for target in command[3:]:
                (self.libdir / f"lib{target}.a").write_bytes(b"archive")

        with mock.patch.object(bootstrap, "run", side_effect=self.fake_llvm_config), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo", "x86"]), \
             mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_ninja):
            bootstrap.ensure_llvm_libraries(self.llvm_config, build=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["ninja", "-C", str(self.root)])
        self.assertEqual(set(calls[0][3:]), {"LLVMMCA", "LLVMX86TargetMCA"})
        self.assertTrue(all(path.is_file() for path in self.absent))

    def test_read_only_check_reports_missing_archives(self) -> None:
        with mock.patch.object(bootstrap, "run", side_effect=self.fake_llvm_config), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo", "x86"]), \
             mock.patch.object(bootstrap.subprocess, "run") as ninja:
            with self.assertRaisesRegex(RuntimeError, "libLLVMMCA.a"):
                bootstrap.ensure_llvm_libraries(self.llvm_config, build=False)
            ninja.assert_not_called()

    def test_unrelated_llvm_config_error_is_not_treated_as_missing_archive(self) -> None:
        def broken_config(*args, **_kwargs):
            if "--libfiles" in args:
                raise RuntimeError("llvm-config failed (1): unknown component")
            return self.fake_llvm_config(*args)

        with mock.patch.object(bootstrap, "run", side_effect=broken_config), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo", "x86"]), \
             mock.patch.object(bootstrap.subprocess, "run") as ninja:
            with self.assertRaisesRegex(RuntimeError, "unknown component"):
                bootstrap.ensure_llvm_libraries(self.llvm_config, build=True)
            ninja.assert_not_called()

    def test_darwin_zstd_search_uses_library_chosen_by_cmake(self) -> None:
        zstd_dir = self.root / "homebrew/lib"
        zstd_dir.mkdir(parents=True)
        (zstd_dir / "libzstd.dylib").write_bytes(b"dylib")
        (self.root / "CMakeCache.txt").write_text(
            f"zstd_LIBRARY:FILEPATH={zstd_dir / 'libzstd.dylib'}\n"
        )

        def fake_config(*args, **_kwargs):
            if "--system-libs" in args:
                return "-lm -lzstd"
            if "--obj-root" in args:
                return str(self.root)
            if args[0] == "pkg-config":
                raise RuntimeError("pkg-config unavailable")
            raise AssertionError(f"unexpected query: {args}")

        with mock.patch.object(bootstrap, "run", side_effect=fake_config), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo"]):
            self.assertEqual(
                bootstrap.darwin_system_library_paths(self.llvm_config, "aarch64-apple-darwin"),
                [zstd_dir.resolve()],
            )


if __name__ == "__main__":
    unittest.main()
