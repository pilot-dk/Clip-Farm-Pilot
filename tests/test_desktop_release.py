from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import desktop_launcher
from scripts.prepare_caption_runtime import detected_architecture


ROOT = Path(__file__).resolve().parents[1]


class DesktopReleaseTests(unittest.TestCase):
    def test_caption_runtime_normalizes_supported_cpu_names(self):
        for machine, expected in (
            ("AMD64", "x64"),
            ("x86_64", "x64"),
            ("ARM64", "arm64"),
            ("aarch64", "arm64"),
        ):
            with self.subTest(machine=machine), mock.patch("platform.machine", return_value=machine):
                self.assertEqual(detected_architecture(), expected)

    def test_release_workflow_builds_and_launches_every_windows_and_linux_architecture(self):
        workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")

        self.assertIn("runner: windows-11-vs2026-arm", workflow)
        self.assertIn("runner: ubuntu-24.04-arm", workflow)
        self.assertGreaterEqual(workflow.count("architecture: arm64"), 2)
        self.assertGreaterEqual(workflow.count("architecture: x64"), 2)
        self.assertIn('CLIPFARMPILOT_TEST_SOURCE = $source', workflow)
        self.assertIn('CLIPFARMPILOT_TEST_WINDOW = "1"', workflow)
        self.assertIn('CLIPFARMPILOT_TEST_SOURCE="$SOURCE"', workflow)
        self.assertIn('CLIPFARMPILOT_TEST_WINDOW=1 xvfb-run', workflow)

    def test_gui_launch_check_cannot_stall_a_release(self):
        workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")
        launcher = (ROOT / "desktop_launcher.py").read_text(encoding="utf-8")

        # WebKitGTK needs software rendering to finish starting on a GPU-less runner.
        self.assertIn("WEBKIT_DISABLE_DMABUF_RENDERER", workflow)
        self.assertIn("WEBKIT_DISABLE_COMPOSITING_MODE", workflow)
        self.assertEqual(workflow.count("timeout-minutes: 45"), 2)
        self.assertEqual(workflow.count("timeout-minutes: 20"), 2)
        # A window that never opens has to end the check instead of holding the job.
        self.assertIn("guard_test_window", launcher)
        self.assertGreater(desktop_launcher.TEST_WINDOW_FAILURE_CODE, 0)
        self.assertGreater(
            desktop_launcher.TEST_WINDOW_TIMEOUT_SECONDS,
            desktop_launcher.TEST_WINDOW_VISIBLE_SECONDS,
        )
        self.assertLess(desktop_launcher.TEST_WINDOW_TIMEOUT_SECONDS, 20 * 60)

    def test_windows_arm64_video_runtime_is_pinned_and_bundled(self):
        preparer = (ROOT / "scripts" / "prepare_windows_arm64_ffmpeg.py").read_text(encoding="utf-8")
        build = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
        launcher = (ROOT / "desktop_launcher.py").read_text(encoding="utf-8")

        # A month-end build: BtbN prunes daily autobuilds after about two weeks.
        self.assertRegex(preparer, r"autobuild-\d{4}-\d{2}-(28|29|30|31)-")
        self.assertRegex(preparer, r'ARCHIVE_SHA256 = "[0-9a-f]{64}"')
        self.assertIn('"$ArmFfmpeg;bin"', build)
        self.assertIn('"$ArmFfprobe;bin"', build)
        self.assertIn("if bundled_ffmpeg.is_file()", launcher)
        self.assertIn("if bundled_ffprobe.is_file()", launcher)


    def test_windows_arm64_bundles_the_dotnet_desktop_runtime_for_its_window(self):
        preparer = (ROOT / "scripts" / "prepare_windows_arm64_dotnet.py").read_text(encoding="utf-8")
        build = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

        self.assertIn('DOTNET_VERSION = "10.0.12"', preparer)
        self.assertIn("dotnet-runtime-{DOTNET_VERSION}-win-arm64.zip", preparer)
        self.assertIn("windowsdesktop-runtime-{DOTNET_VERSION}-win-arm64.zip", preparer)
        self.assertEqual(len(__import__("re").findall(r'"[0-9a-f]{64}"', preparer)), 2)
        self.assertIn("Microsoft.WindowsDesktop.App", preparer)
        self.assertIn('"$ArmDotnet;dotnet"', build)
        self.assertIn(".NET 10 runtime and Windows Desktop runtime", notices)

    def test_windows_build_and_test_stop_on_any_failing_program(self):
        build = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")

        for step in (
            "Installing Python dependencies",
            "Preparing the offline live-caption engine",
            "Preparing the Windows ARM64 video tools",
            "Preparing the .NET desktop runtime",
            "Packaging the app",
        ):
            self.assertIn(f'Assert-Success "{step}"', build)
        self.assertIn("The packaged video pipeline test failed with exit code", workflow)
        self.assertIn("The packaged window launch test failed with exit code", workflow)
        # Piping makes PowerShell wait for the windowed app and record its exit code.
        self.assertEqual(workflow.count('ClipFarmPilot.exe" | Out-Host'), 2)

    def test_pywebview_patch_only_applies_to_the_version_it_was_written_for(self):
        from scripts import patch_pywebview_for_modern_dotnet as patcher

        build = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
        arm64_section = build[build.index('if ($Architecture -eq "arm64")'):build.index("$PyInstallerArgs = @(")]
        self.assertIn("scripts/patch_pywebview_for_modern_dotnet.py", arm64_section)

        self.assertEqual(patcher.PYWEBVIEW_VERSION, "6.2.1")
        self.assertIn("pywebview==6.2.1", (ROOT / "desktop-requirements.txt").read_text(encoding="utf-8"))
        with mock.patch("importlib.metadata.version", return_value="6.3.0"):
            with self.assertRaisesRegex(RuntimeError, "written for pywebview 6.2.1"):
                patcher.main()

    def test_pywebview_patch_defers_framework_reflection_and_falls_back(self):
        from scripts import patch_pywebview_for_modern_dotnet as patcher

        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "winforms.py"
            module.write_text("import os\n\n\n" + patcher.ORIGINAL + "        return None\n", encoding="utf-8")
            with mock.patch("importlib.metadata.version", return_value="6.2.1"), mock.patch.object(
                patcher, "winforms_module_path", return_value=module
            ):
                patcher.main()
                patched = module.read_text(encoding="utf-8")
                patcher.main()  # a second build run leaves it unchanged
                self.assertEqual(module.read_text(encoding="utf-8"), patched)
                module.write_text("class Unexpected:\n    pass\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                    patcher.main()

        class_body = patched[patched.index("class OpenFolderDialog:"):patched.index("def show(")]
        # Nothing reflects into WinForms internals while the module is imported.
        self.assertNotIn("iFileDialogType =", class_body.split("def _load_framework_internals")[0])
        self.assertIn("if iFileDialogType is None:", patched)
        self.assertIn("WinForms.FolderBrowserDialog()", patched)
        compile(patched, "winforms.py", "exec")

    def test_arm64_whisper_is_compiled_with_clang_cl(self):
        caption_runtime = (ROOT / "scripts" / "prepare_caption_runtime.py").read_text(encoding="utf-8")
        self.assertIn('"-A", "ARM64", "-T", "ClangCL"', caption_runtime)
        self.assertIn('"-DGGML_NATIVE=OFF"', caption_runtime)


class WindowsArm64DotnetLoaderTests(unittest.TestCase):
    def _bundle(self, root: Path, with_config: bool = True) -> Path:
        dotnet = root / "dotnet"
        dotnet.mkdir(parents=True)
        if with_config:
            (dotnet / "clipfarmpilot.runtimeconfig.json").write_text("{}", encoding="utf-8")
        return dotnet

    def _run_loader(self, platform_name: str, machine: str, resource_dir: Path, clr_module=None):
        fake_pythonnet = types.ModuleType("pythonnet")
        fake_pythonnet.load = mock.Mock()
        fake_clr = clr_module or types.ModuleType("clr")
        if not hasattr(fake_clr, "AddReference"):
            fake_clr.AddReference = mock.Mock()
        with mock.patch.object(desktop_launcher.sys, "platform", platform_name), mock.patch.object(
            desktop_launcher.platform, "machine", return_value=machine
        ), mock.patch.object(desktop_launcher, "_resource_dir", return_value=resource_dir), mock.patch.dict(
            sys.modules, {"pythonnet": fake_pythonnet, "clr": fake_clr}
        ):
            loaded = desktop_launcher._load_windows_arm64_dotnet()
        return loaded, fake_pythonnet.load

    def test_windows_arm64_starts_the_bundled_desktop_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dotnet = self._bundle(root)
            loaded, load = self._run_loader("win32", "ARM64", root)
        self.assertTrue(loaded)
        load.assert_called_once_with(
            "coreclr",
            runtime_config=str(dotnet / "clipfarmpilot.runtimeconfig.json"),
            dotnet_root=str(dotnet),
        )

    def test_every_assembly_pywebview_imports_from_is_referenced_after_loading(self):
        fake_clr = types.ModuleType("clr")
        fake_clr.AddReference = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._bundle(root)
            self._run_loader("win32", "ARM64", root, clr_module=fake_clr)
        referenced = [call.args[0] for call in fake_clr.AddReference.call_args_list]
        # Mapped from the .NET 10 metadata of each type pywebview's Windows backend imports.
        for assembly in (
            "System.Windows.Forms",
            "Microsoft.Win32.SystemEvents",
            "System.Drawing.Primitives",
            "System.Drawing.Common",
            "System.Private.Uri",
            "System.Diagnostics.Process",
        ):
            self.assertIn(assembly, referenced)

    def test_other_platforms_and_unbundled_runs_leave_pywebview_to_choose(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundled = Path(temporary) / "bundled"
            self._bundle(bundled)
            unbundled = Path(temporary) / "unbundled"
            self._bundle(unbundled, with_config=False)
            for platform_name, machine, root in (
                ("win32", "AMD64", bundled),   # x64 keeps the built-in .NET Framework
                ("darwin", "arm64", bundled),
                ("linux", "aarch64", bundled),
                ("win32", "ARM64", unbundled),  # a source checkout without the runtime
            ):
                with self.subTest(platform=platform_name, machine=machine, root=root.name):
                    loaded, load = self._run_loader(platform_name, machine, root)
                    self.assertFalse(loaded)
                    load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
