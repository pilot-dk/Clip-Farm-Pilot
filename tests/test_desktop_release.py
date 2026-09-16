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

    def _run_loader(self, platform_name: str, machine: str, resource_dir: Path):
        fake_pythonnet = types.ModuleType("pythonnet")
        fake_pythonnet.load = mock.Mock()
        with mock.patch.object(desktop_launcher.sys, "platform", platform_name), mock.patch.object(
            desktop_launcher.platform, "machine", return_value=machine
        ), mock.patch.object(desktop_launcher, "_resource_dir", return_value=resource_dir), mock.patch.dict(
            sys.modules, {"pythonnet": fake_pythonnet}
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
