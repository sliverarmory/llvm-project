"""Regression tests for Rust bootstrap preflight failures seen in milestone 0."""

from __future__ import annotations

import importlib.util
import os
import shlex
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

    def test_native_host_triples_include_linux_arm64_and_windows_msvc(self) -> None:
        cases = (
            ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
            ("Linux", "arm64", "aarch64-unknown-linux-gnu"),
            ("Windows", "AMD64", "x86_64-pc-windows-msvc"),
        )
        for system, machine, expected in cases:
            with self.subTest(system=system, machine=machine), \
                 mock.patch.object(bootstrap.platform, "system", return_value=system), \
                 mock.patch.object(bootstrap.platform, "machine", return_value=machine):
                self.assertEqual(bootstrap.host_triple(), expected)

    def test_linux_arm64_accepts_aarch64_codegen_build(self) -> None:
        self.llvm_config.parent.mkdir()
        self.llvm_config.write_bytes(b"tool")
        (self.root / "CMakeCache.txt").write_text(
            f"CMAKE_HOME_DIRECTORY:INTERNAL={bootstrap.ROOT / 'llvm'}\n"
        )

        def fake_config(*args, **_kwargs):
            if "--version" in args:
                return bootstrap.PINS["llvm_version"]
            if "--assertion-mode" in args:
                return "ON"
            if "--targets-built" in args:
                return "AArch64 X86"
            if "--obj-root" in args:
                return str(self.root)
            raise AssertionError(args)

        with mock.patch.object(bootstrap, "run", side_effect=fake_config):
            bootstrap.check_llvm(self.llvm_config, "aarch64-unknown-linux-gnu")

    def test_windows_missing_libraries_build_native_lib_targets(self) -> None:
        self.libdir = self.root / "LLVM Build" / "lib"
        self.libdir.mkdir(parents=True)
        self.absent = [self.libdir / "LLVMMCA.lib", self.libdir / "LLVMX86TargetMCA.lib"]
        for name in ("LLVMPasses.lib", "LLVMObfuscation.lib"):
            (self.libdir / name).write_bytes(b"archive")
        calls = []

        def quoted_paths(paths):
            return " ".join(f'"{path}"' for path in paths)

        def fake_config(*args, **_kwargs):
            if "--libdir" in args:
                return str(self.libdir)
            if "--obj-root" in args:
                return str(self.root)
            if "--libfiles" in args:
                missing = [path for path in self.absent if not path.exists()]
                if missing:
                    raise RuntimeError("llvm-config failed (1): " + "\n".join(
                        f"llvm-config: error: missing: {path}" for path in missing))
                return quoted_paths([*self.absent, self.libdir / "LLVMPasses.lib",
                                     self.libdir / "LLVMObfuscation.lib"])
            if "--libs" in args:
                return quoted_paths([*self.absent, self.libdir / "LLVMPasses.lib",
                                     self.libdir / "LLVMObfuscation.lib"])
            raise AssertionError(args)

        def fake_ninja(command, **_kwargs):
            calls.append(command)
            for target in command[3:]:
                (self.libdir / f"{target}.lib").write_bytes(b"archive")

        with mock.patch.object(bootstrap.platform, "system", return_value="Windows"), \
             mock.patch.object(bootstrap, "run", side_effect=fake_config), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo", "x86"]), \
             mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_ninja):
            bootstrap.ensure_llvm_libraries(self.llvm_config, build=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0][3:]), {"LLVMMCA", "LLVMX86TargetMCA"})

    def test_llvm_quote_paths_roundtrips_windows_backslashes(self) -> None:
        # llvm::sys::printArg escapes each backslash inside quoted paths.
        output = r'"C:\\LLVM Build\\lib\\LLVMObfuscation.lib"'
        self.assertEqual(shlex.split(output), [r"C:\LLVM Build\lib\LLVMObfuscation.lib"])

    def test_windows_missing_non_llvm_library_is_not_built(self) -> None:
        def fake_config(*args, **_kwargs):
            if "--libdir" in args:
                return str(self.libdir)
            if "--obj-root" in args:
                return str(self.root)
            if "--libfiles" in args:
                raise RuntimeError(
                    f"llvm-config failed (1): llvm-config: error: missing: {self.libdir / 'Other.lib'}"
                )
            raise AssertionError(args)

        with mock.patch.object(bootstrap.platform, "system", return_value="Windows"), \
             mock.patch.object(bootstrap, "run", side_effect=fake_config), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo", "x86"]), \
             mock.patch.object(bootstrap.subprocess, "run") as ninja:
            with self.assertRaisesRegex(RuntimeError, "unexpected llvm-config missing library"):
                bootstrap.ensure_llvm_libraries(self.llvm_config, build=True)
            ninja.assert_not_called()

    def test_windows_llvm_tools_require_exe_files_but_ninja_uses_target_names(self) -> None:
        bindir = self.root / "bin"
        bindir.mkdir()
        missing_target = bootstrap.LLVM_TOOLS[0]
        for name in bootstrap.LLVM_TOOLS[1:]:
            (bindir / f"{name}.exe").write_bytes(b"exe")

        def fake_ninja(command, **_kwargs):
            self.assertEqual(command, ["ninja", "-C", str(self.root), missing_target])
            (bindir / f"{missing_target}.exe").write_bytes(b"exe")

        with mock.patch.object(bootstrap.platform, "system", return_value="Windows"), \
             mock.patch.object(bootstrap, "run", side_effect=lambda *_args: str(bindir)
                               if "--bindir" in _args else str(self.root)), \
             mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_ninja):
            bootstrap.ensure_llvm_tools(self.llvm_config, build=True)

    def test_windows_msvc_requires_developer_environment_for_prepare(self) -> None:
        with mock.patch.dict(os.environ, {"INCLUDE": "", "LIB": ""}), \
             mock.patch.object(bootstrap.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "vcvars64.bat"):
                bootstrap.check_msvc_environment("x86_64-pc-windows-msvc")
        with mock.patch.dict(os.environ, {"INCLUDE": "headers", "LIB": "libraries"}), \
             mock.patch.object(bootstrap.shutil, "which", return_value="cl-or-link.exe"):
            bootstrap.check_msvc_environment("x86_64-pc-windows-msvc")
        with mock.patch.dict(os.environ, {"VSCMD_ARG_TGT_ARCH": "x86"}):
            with self.assertRaisesRegex(RuntimeError, "got x86 target"):
                bootstrap.check_msvc_environment("x86_64-pc-windows-msvc")

    def test_windows_system_import_libraries_extend_msvc_lib_path(self) -> None:
        zstd_dir = self.root / "vcpkg installed" / "lib"
        zstd_dir.mkdir(parents=True)
        zstd = zstd_dir / "zstd.lib"
        zstd.write_bytes(b"import library")

        with mock.patch.object(bootstrap, "run", return_value=f'"{zstd}" kernel32.lib'), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo"]):
            self.assertEqual(
                bootstrap.windows_system_library_paths(self.llvm_config, "x86_64-pc-windows-msvc"),
                [zstd_dir.resolve()],
            )

    def test_windows_bare_zstd_library_uses_cmake_cache_path(self) -> None:
        zstd_dir = self.root / "vcpkg" / "lib"
        zstd_dir.mkdir(parents=True)
        zstd = zstd_dir / "zstd.lib"
        zstd.write_bytes(b"import library")
        (self.root / "CMakeCache.txt").write_text(f"zstd_LIBRARY:FILEPATH={zstd}\n")

        def fake_config(*args, **_kwargs):
            if "--system-libs" in args:
                return "zstd.lib kernel32.lib"
            if "--obj-root" in args:
                return str(self.root)
            raise AssertionError(args)

        with mock.patch.object(bootstrap, "run", side_effect=fake_config), \
             mock.patch.object(bootstrap, "rust_llvm_components", return_value=["ipo"]):
            self.assertEqual(
                bootstrap.windows_system_library_paths(self.llvm_config, "x86_64-pc-windows-msvc"),
                [zstd_dir.resolve()],
            )


if __name__ == "__main__":
    unittest.main()
