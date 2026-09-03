from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

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

    def test_windows_arm64_video_runtime_is_pinned_and_bundled(self):
        preparer = (ROOT / "scripts" / "prepare_windows_arm64_ffmpeg.py").read_text(encoding="utf-8")
        build = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
        launcher = (ROOT / "desktop_launcher.py").read_text(encoding="utf-8")

        self.assertIn("autobuild-2026-09-03-13-17", preparer)
        self.assertRegex(preparer, r'ARCHIVE_SHA256 = "[0-9a-f]{64}"')
        self.assertIn('"$ArmFfmpeg;bin"', build)
        self.assertIn('"$ArmFfprobe;bin"', build)
        self.assertIn("if bundled_ffmpeg.is_file()", launcher)
        self.assertIn("if bundled_ffprobe.is_file()", launcher)


if __name__ == "__main__":
    unittest.main()
