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

    # Auto length

    @staticmethod
    def build_up(duration: int, start: int, end: int, strength: float = 0.9) -> AudioAnalysis:
        """Energy that climbs steadily from start to a payoff at end."""
        audio = audio_track(duration, [])
        curve = np.linspace(0.05, strength, end - start, dtype=np.float32)
        audio.rms[start:end] = curve
        audio.peak[start:end] = np.clip(curve * 1.35, 0, 1)
        audio.texture[start:end] = curve * 0.45
        audio.burst[end - 2] = strength
        return audio

    def test_auto_length_follows_the_moment(self):
        reaction = self.analyze(240, audio_track(240, [(91, 101, 0.85)]), target="auto", limit=1)[0]
        build_up = self.analyze(240, self.build_up(240, 100, 125), target="auto", limit=1)[0]

        self.assertLess(reaction["end"] - reaction["start"], 20)
        self.assertGreater(build_up["end"] - build_up["start"], reaction["end"] - reaction["start"] + 5)
        # The whole climb is in the clip, and it ends as the payoff lands.
        self.assertLessEqual(build_up["start"], 100)
        self.assertGreaterEqual(build_up["end"], 125)
        self.assertLess(build_up["end"], 129)

    def test_auto_clips_stay_between_ten_and_sixty_seconds_with_setup(self):
        for audio in (
            audio_track(240, [(98, 100, 0.95)]),     # A two-second spike.
            audio_track(240, [(80, 150, 0.8)]),      # Seventy seconds of hype.
            audio_track(240, [(55, 65, 0.8), (182, 194, 0.95)]),
        ):
            for clip in self.analyze(240, audio, target="auto", limit=3):
                self.assertGreaterEqual(clip["end"] - clip["start"], 10.0, clip)
                self.assertLessEqual(clip["end"] - clip["start"], 60.0, clip)
                self.assertGreaterEqual(clip["peak"] - clip["start"], 6.0, clip)

    def test_auto_clips_do_not_repeat_each_other(self):
        audio = audio_track(300, [(60, 68, 0.9), (95, 120, 0.8), (104, 108, 0.95), (220, 230, 0.85)])
        clips = self.analyze(300, audio, target="auto", limit=5)

        for index, first in enumerate(clips):
            for second in clips[index + 1:]:
                overlap = max(0.0, min(first["end"], second["end"]) - max(first["start"], second["start"]))
                shorter = min(first["end"] - first["start"], second["end"] - second["start"])
                self.assertLessEqual(overlap / shorter, 0.40, (first, second))

    def test_auto_on_a_quiet_vod_uses_a_typical_length(self):
        quiet = AudioAnalysis(*(np.zeros(180, dtype=np.float32) for _ in range(4)))

        def visual(_path, start, end):
            return (0.96, 0.88) if start <= 88 <= end else (0.03, 0.0)

        best = self.analyze(180, quiet, visual=visual, target="auto", limit=3)[0]
        self.assertAlmostEqual(best["end"] - best["start"], 30.0, places=2)
        self.assertLessEqual(best["start"], 88)
        self.assertGreaterEqual(best["end"], 88)

    def test_the_api_accepts_auto_as_a_length(self):
        from pydantic import ValidationError

        from backend.app.main import AnalyzeRequest

        self.assertEqual(AnalyzeRequest(target_duration="auto").target_duration, "auto")
        self.assertEqual(AnalyzeRequest(target_duration=45).target_duration, 45)
        for invalid in (5, 120, "long"):
            with self.assertRaises(ValidationError):
                AnalyzeRequest(target_duration=invalid)


if __name__ == "__main__":
    unittest.main()
