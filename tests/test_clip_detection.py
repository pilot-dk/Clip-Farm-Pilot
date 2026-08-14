from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.app.video import AudioAnalysis, VideoInfo, analyze_viral_candidates


def audio_track(duration: int, events: list[tuple[int, int, float]]) -> AudioAnalysis:
    rms = np.full(duration, 0.025, dtype=np.float32)
    peak = np.full(duration, 0.04, dtype=np.float32)
    burst = np.full(duration, 0.004, dtype=np.float32)
    texture = np.full(duration, 0.008, dtype=np.float32)
    for start, end, strength in events:
        rms[start:end] = strength * np.linspace(0.55, 1.0, max(1, end - start), dtype=np.float32)
        peak[start:end] = np.clip(rms[start:end] * 1.35, 0, 1)
        burst[min(end - 1, start + max(1, (end - start) // 2))] = strength
        texture[start:end] = strength * 0.45
    return AudioAnalysis(rms=rms, peak=peak, burst=burst, texture=texture)


class ClipDetectionTests(unittest.TestCase):
    def analyze(self, duration: int, audio: AudioAnalysis, visual=None, target: int = 30, limit: int = 5):
        visual = visual or (lambda _path, _start, _end: (0.25, 0.12))
        with (
            patch("backend.app.video.probe_video", return_value=VideoInfo(1920, 1080, float(duration))),
            patch("backend.app.video._audio_analysis_per_second", return_value=audio),
            patch("backend.app.video._visual_window_summary", side_effect=visual),
        ):
            return analyze_viral_candidates(Path("synthetic-vod.mp4"), target, limit)

    def test_strong_reaction_is_ranked_with_context_before_the_payoff(self):
        results = self.analyze(180, audio_track(180, [(91, 101, 0.85)]), target=30, limit=3)

        best = results[0]
        self.assertLessEqual(best["start"], 91)
        self.assertGreaterEqual(best["end"], 100)
        self.assertGreater(best["peak"] - best["start"], 15)
        self.assertLess(best["peak"] - best["start"], 26)
        self.assertIn(best["label"], {"Reaction + payoff", "Big reaction", "Build-up"})
        self.assertGreater(best["signals"]["reaction"], 40)

    def test_separate_highlights_survive_diversity_filter(self):
        results = self.analyze(
            260,
            audio_track(260, [(55, 65, 0.8), (182, 194, 0.95)]),
            target=30,
            limit=3,
        )

        peaks = [result["peak"] for result in results]
        self.assertTrue(any(50 <= peak <= 70 for peak in peaks), peaks)
        self.assertTrue(any(178 <= peak <= 198 for peak in peaks), peaks)

    def test_nearby_reaction_spikes_do_not_create_duplicate_clips(self):
        audio = audio_track(180, [(76, 82, 0.84), (84, 91, 0.92), (140, 149, 0.7)])
        results = self.analyze(180, audio, target=30, limit=5)

        clustered = [result for result in results if 70 <= result["peak"] <= 100]
        self.assertEqual(len(clustered), 1, results)

    def test_quiet_vod_uses_visual_coverage_across_the_full_recording(self):
        quiet = AudioAnalysis(*(np.zeros(180, dtype=np.float32) for _ in range(4)))

        def visual(_path, start, end):
            return (0.96, 0.88) if start <= 88 <= end else (0.03, 0.0)

        results = self.analyze(180, quiet, visual=visual, target=30, limit=3)

        best = results[0]
        self.assertLessEqual(best["start"], 88)
        self.assertGreaterEqual(best["end"], 88)
        self.assertEqual(best["label"], "Fast action")
        self.assertGreater(best["signals"]["visual"], 90)

    def test_candidate_contract_is_bounded_and_explained(self):
        results = self.analyze(120, audio_track(120, [(50, 61, 0.9)]), target=15, limit=2)

        self.assertLessEqual(len(results), 2)
        for result in results:
            self.assertGreaterEqual(result["score"], 1)
            self.assertLessEqual(result["score"], 98)
            self.assertGreater(result["end"], result["start"])
            self.assertTrue(result["label"])
            self.assertTrue(result["reason"])
            self.assertEqual(set(result["signals"]), {"reaction", "momentum", "visual", "contrast"})

    def test_short_vod_handles_analysis_windows_larger_than_its_signal(self):
        results = self.analyze(7, audio_track(7, [(3, 6, 0.7)]), target=15, limit=3)

        self.assertTrue(results)
        self.assertEqual(results[0]["start"], 0.0)
        self.assertEqual(results[0]["end"], 7.0)


if __name__ == "__main__":
    unittest.main()
