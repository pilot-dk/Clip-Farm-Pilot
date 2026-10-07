from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from backend.app import highlights, video
from backend.app.captions import CaptionWord
from backend.app.highlights import (
    action_keep_spans,
    card_events,
    card_matches,
    fight_spans,
    game_events,
    hud_events,
    hud_gone_stretches,
    is_game_callout,
    kill_spans,
    shots_from_counter,
    talking_spans,
)

FPS = highlights.SHOT_FPS


class ShotTests(unittest.TestCase):
    """Counter readings ten times a second: magazine and reserve change, and how white each is."""

    def readings(self, seconds: float = 60.0):
        count = int(seconds * FPS)
        rng = np.random.default_rng(4)
        return {
            "magazine_change": rng.uniform(0.0, 0.006, count).astype(np.float32),
            "reserve_change": rng.uniform(0.0, 0.004, count).astype(np.float32),
            "magazine_white": np.full(count, 0.08, dtype=np.float32),
            "reserve_white": np.full(count, 0.05, dtype=np.float32),
        }

    @staticmethod
    def burst(readings, second: float, length: float = 1.0):
        first = int(second * FPS)
        readings["magazine_change"][first:first + int(length * FPS)] = 0.06

    def test_bursts_of_fire_and_single_shots_are_found(self):
        readings = self.readings()
        for second in (5, 15, 25, 35, 45):
            self.burst(readings, second)
        readings["magazine_change"][int(52.0 * FPS)] = 0.06  # One sniper shot between steady numbers.
        shots = shots_from_counter(**readings)

        self.assertTrue(all(any(abs(shot - second - 0.5) <= 0.5 for shot in shots) for second in (5, 15, 25, 35, 45)))
        self.assertIn(52.0, shots)
        self.assertTrue(all(any(abs(shot - s - 0.5) <= 0.6 for s in (5, 15, 25, 35, 45)) or shot == 52.0 for shot in shots))

    def test_scenery_behind_the_counter_and_blinking_digits_are_not_shots(self):
        readings = self.readings()
        for second in (5, 15, 25, 35, 45):
            self.burst(readings, second)
        # Something bright passes behind both halves of the counter.
        readings["magazine_change"][300:310] = 0.08
        readings["reserve_change"][300:310] = 0.08
        # The low-ammo warning turns the digits red: they vanish from white.
        readings["magazine_change"][540] = 0.06
        readings["magazine_white"][541:] = 0.0
        shots = shots_from_counter(**readings)

        self.assertFalse(any(30.0 <= shot <= 31.0 for shot in shots))
        self.assertNotIn(54.0, shots)

    def test_a_video_without_a_counter_gives_no_shots(self):
        readings = self.readings()
        for second in (5, 15, 25, 35, 45):
            self.burst(readings, second)
        no_reserve = dict(readings, reserve_white=np.zeros_like(readings["reserve_white"]))
        self.assertEqual(shots_from_counter(**no_reserve), [])
        busy_reserve = dict(readings, reserve_change=np.full_like(readings["reserve_change"], 0.02))
        self.assertEqual(shots_from_counter(**busy_reserve), [])
        too_few = self.readings()
        self.burst(too_few, 10)
        self.assertEqual(shots_from_counter(**too_few), [])


def scenery(count: int, low: int = 40, high: int = 150, seed: int = 1) -> np.ndarray:
    """Card boxes of soft shapes, like the game world behind the HUD."""
    rng = np.random.default_rng(seed)
    small = rng.integers(low, high, (count, 6, 12)).astype(np.uint8)
    return np.stack([np.asarray(Image.fromarray(frame).resize((154, 65), Image.BILINEAR)) for frame in small])


def paint(frames: np.ndarray, rows: tuple[str, ...], at: tuple[int, int], shift: tuple[int, int] = (0, 0)) -> np.ndarray:
    """Draw a label picture's bright strokes over the frames, the way the game draws its text."""
    glyph = (np.array([[int(level) for level in row] for row in rows], dtype=np.float32) * 28).astype(np.uint8)
    height, width = glyph.shape
    top, left = at[0] + shift[0], at[1] + shift[1]
    frames[:, top:top + height, left:left + width] = np.maximum(frames[:, top:top + height, left:left + width], glyph)
    return frames


class CardTests(unittest.TestCase):
    def test_kill_and_death_cards_are_told_from_scenery_and_each_other(self):
        kill = paint(scenery(20), highlights._KILLED_PICTURE, highlights._KILLED_AT, (2, -2))
        death = paint(scenery(20), highlights._KILLED_PICTURE, highlights._KILLED_AT)
        death = paint(death, highlights._BY_PICTURE, highlights._BY_AT)
        over_bright = paint(scenery(20, 150, 200), highlights._KILLED_PICTURE, highlights._KILLED_AT, (-3, 3))

        for frames in (kill, over_bright):
            killed, by = card_matches(frames)
            self.assertTrue((killed >= highlights._LABEL_MATCH).all())
            self.assertLess(np.median(by), highlights._NO_BY)
        killed, by = card_matches(death)
        self.assertTrue((killed >= highlights._LABEL_MATCH).all())
        self.assertTrue((by >= highlights._BY_MATCH).all())
        for frames in (scenery(50, seed=2), scenery(50, 170, 230, seed=3)):
            killed, by = card_matches(frames)
            self.assertTrue((killed < 0.5).all())
            self.assertTrue((by == -1).all())
        self.assertEqual(len(card_matches(np.zeros((0, 65, 154), dtype=np.uint8))[0]), 0)

    def test_cards_become_kills_and_deaths_at_the_moment_they_show(self):
        killed = np.zeros(300, dtype=np.float32)
        by = np.full(300, -1.0, dtype=np.float32)
        killed[50:74] = 0.9                     # A kill card...
        killed[60:64] = 0.2                     # ...that drops out in a flash.
        by[50:74] = 0.1
        killed[120:122] = 0.9                   # A two-frame glimpse is not a card.
        by[120:122] = 0.0
        killed[200:244] = 0.9                   # A kill, and the player dies straight after.
        by[200:220] = 0.05
        by[220:244] = 0.8
        killed[260:270] = 0.9                   # Neither "By" nor clearly not: no card is called.
        by[260:270] = 0.38

        kills, deaths = card_events(killed, by)

        self.assertEqual(kills, [5.0, 20.0])
        self.assertEqual(deaths, [22.0])


class HudGoneTests(unittest.TestCase):
    def test_a_second_without_the_counter_is_out_of_live_play(self):
        magazine = np.full(200, 0.08, dtype=np.float32)
        reserve = np.full(200, 0.05, dtype=np.float32)
        magazine[50:70] = reserve[50:70] = 0.0      # Dead for two seconds.
        magazine[100:105] = reserve[100:105] = 0.0  # A flicker.
        magazine[150:170] = 0.0                     # Low ammo turns the magazine red; the reserve stays.

        self.assertEqual(hud_gone_stretches(magazine, reserve), [(5.0, 7.0)])

    def test_the_counters_absence_means_nothing_in_a_game_without_one(self):
        nothing = {
            name: np.zeros(600, dtype=np.float32)
            for name in ("magazine_change", "reserve_change", "magazine_white", "reserve_white", "killed")
        }
        nothing["by"] = np.full(600, -1.0, dtype=np.float32)

        self.assertEqual(game_events(nothing), highlights.GameEvents())


class FightTests(unittest.TestCase):
    def test_a_fight_keeps_its_run_up_and_its_landing(self):
        spans = fight_spans([10.0, 10.1, 10.2, 12.0, 30.0], 60.0)
        self.assertEqual(spans, [(7.5, 14.0), (27.5, 32.0)])

    def test_a_hard_cut_ends_the_landing_early(self):
        motion = np.full(240, 0.4, dtype=np.float32)
        motion[int(11.0 * 4)] = 0.95  # The death camera cuts in a second after the last shot.
        (start, end), = fight_spans([10.0], 60.0, motion)
        self.assertEqual(start, 7.5)
        self.assertAlmostEqual(end, 11.0, places=2)

    def test_a_fight_stays_inside_live_play(self):
        # The player respawned at 8.5 s and died, the counter gone, at 12.5 s.
        spans = fight_spans([10.0, 11.0], 60.0, hud_gone=[(0.0, 8.5), (12.5, 20.0)])
        self.assertEqual(spans, [(8.5, 12.5)])

    def test_a_kill_keeps_its_approach_and_its_card(self):
        self.assertEqual(kill_spans([20.0], [], 60.0), [(16.5, 21.5)])
        # A fight leads into the kill: its first shots and their run-up are kept.
        self.assertEqual(kill_spans([20.0], [15.0, 15.1, 18.0, 19.6], 60.0), [(13.5, 21.5)])
        # Shots long before, or long after the shots stopped, are another matter.
        self.assertEqual(kill_spans([20.0], [8.0, 8.1, 14.0], 60.0), [(16.5, 21.5)])
        # A fight that went on and on is kept from eight seconds before the kill.
        self.assertEqual(kill_spans([20.0], [2.0, 5.0, 8.0, 11.0, 14.0, 17.0, 19.8], 60.0), [(12.0, 21.5)])

    def test_a_kill_stays_inside_live_play(self):
        self.assertEqual(kill_spans([20.0], [], 60.0, hud_gone=[(10.0, 18.0)]), [(18.0, 21.5)])
        self.assertEqual(kill_spans([20.0], [], 60.0, hud_gone=[(21.0, 30.0)]), [(16.5, 21.0)])
        self.assertEqual(kill_spans([1.0], [], 1.8), [(0.0, 1.8)])

    def test_moments_and_talking_are_joined_and_slivers_dropped(self):
        kept = action_keep_spans([(7.5, 14.0), (27.5, 32.0)], [(14.8, 18.0), (40.0, 40.9)], 60.0)
        self.assertEqual(kept, [(7.5, 18.0), (27.5, 32.0)])


class TalkingTests(unittest.TestCase):
    @staticmethod
    def said(text: str, start: float) -> list[CaptionWord]:
        return [CaptionWord(word, round(start + 0.3 * i, 2), round(start + 0.3 * i + 0.25, 2)) for i, word in enumerate(text.split())]

    def test_the_games_voice_lines_are_not_the_creator_talking(self):
        for line in ("UAV inbound.", "You av inbound.", "Concussion out!", "Kill confirmed.", "We've lost the advantage.",
                     "Hostile care package inbound.", "Cover me! I'm reloading!", "Be advised, hostile UAV incoming.",
                     "Friendly hunter killer drone deployed.", "The advice hostile counter UAP is online.",
                     "Alright, get out there. You know what to do.", "Move in and take 'em out.", "Okay, confirm.",
                     "Don't stop until they're all dead.", "Keep pressing the fight, we're ahead."):
            self.assertTrue(is_game_callout(line), line)
        for line in ("What's up chat, how you doing?", "I'm gonna kill him.", "Uh, I just dropped him.", "That was bad.",
                     "He's above me.", "I got smoked.", "Get out of my way, bro.", "I didn't see him, we're good."):
            self.assertFalse(is_game_callout(line), line)

    def test_phrases_with_substance_are_kept_with_a_breath(self):
        words = self.said("What's up chat how you doing", 10.0) + self.said("UAV inbound", 20.0) + self.said("No", 30.0)
        self.assertEqual(talking_spans(words, 60.0), [(9.65, round(10.0 + 0.3 * 5 + 0.25 + 0.45, 2))])


def write_hud_video(
    path: Path, bursts: list[float], seconds: float, kills: tuple[float, ...] = (), deaths: tuple[float, ...] = (),
    hidden: tuple[tuple[float, float], ...] = (), counter: bool = True,
) -> None:
    """A 720p shooter: a "40/89" counter that drops through each burst, and Black Ops II style kill cards."""
    width, height, rate = 1280, 720, 20
    scale = height / 1080
    x, y, w, h = highlights._COUNTER_BOX
    box = (round(x * width), round(y * height), round(w * width), round(h * height))
    font = ImageFont.load_default(size=32)

    def label(rows: tuple[str, ...]) -> np.ndarray:
        image = Image.fromarray((np.array([[int(level) for level in row] for row in rows]) * 28).astype(np.uint8))
        return np.asarray(image.resize((round(image.width * scale), round(image.height * scale)), Image.BILINEAR))

    killed, by = label(highlights._KILLED_PICTURE), label(highlights._BY_PICTURE)
    card_left, card_top = highlights._CARD_BOX[0] * width, highlights._CARD_BOX[1] * height

    def place(frame: np.ndarray, picture: np.ndarray, at: tuple[int, int]) -> None:
        top, left = round(card_top + at[0] * scale), round(card_left + at[1] * scale)
        rows, columns = picture.shape
        frame[top:top + rows, left:left + columns] = np.maximum(frame[top:top + rows, left:left + columns], picture[:, :, None])

    # Soft shapes drifting past, darker than the digits, like a game world behind the HUD.
    rng = np.random.default_rng(2)
    backdrop = np.asarray(
        Image.fromarray(rng.integers(40, 150, (12, 48, 3), dtype=np.uint8)).resize((width * 2, height), Image.BILINEAR)
    )
    process = subprocess.Popen(
        [video.ffmpeg_executable(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
         "-r", str(rate), "-i", "pipe:0", "-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={seconds}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        stdin=subprocess.PIPE,
    )
    ammo = 40
    for index in range(int(seconds * rate)):
        t = index / rate
        shift = (index * 7) % width
        frame = np.ascontiguousarray(backdrop[:, shift:shift + width])
        if any(start <= t < start + 2.0 for start in (*kills, *deaths)):
            place(frame, killed, highlights._KILLED_AT)
            if any(start <= t < start + 2.0 for start in deaths):
                place(frame, by, highlights._BY_AT)
        image = Image.fromarray(frame)
        if counter and not any(start <= t < end for start, end in hidden):
            if any(start <= t < start + 1.0 for start in bursts) and index % 2 == 0:
                ammo = ammo - 1 if ammo > 1 else 40
            draw = ImageDraw.Draw(image)
            draw.text((box[0] + 5, box[1] + 3), f"{ammo}", fill=(255, 255, 255), font=font)
            draw.text((box[0] + box[2] // 2 + 5, box[1] + 3), "/89", fill=(255, 255, 255), font=font)
        process.stdin.write(image.tobytes())
    process.stdin.close()
    process.wait()


class HudVideoTests(unittest.TestCase):
    BURSTS = [4.0, 12.0, 20.0, 28.0, 36.0, 44.0]
    KILLS = (4.8, 20.8, 36.8)

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls._folder.name)
        cls.shooter = cls.root / "shooter.mp4"
        write_hud_video(cls.shooter, cls.BURSTS, 52.0, kills=cls.KILLS, deaths=(45.0,), hidden=((47.0, 50.0),))
        cls.no_cards = cls.root / "no-cards.mp4"
        write_hud_video(cls.no_cards, cls.BURSTS, 52.0)
        cls.plain = cls.root / "plain.mp4"
        write_hud_video(cls.plain, cls.BURSTS, 20.0, kills=(4.0,), counter=False)

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def export(self, source: Path, seconds: float) -> tuple[dict, float]:
        output = self.root / f"{source.stem}-action.mp4"
        metadata: dict[str, object] = {}
        with patch.object(video, "transcribe_words", return_value=[]):
            video.export_clip(
                source=source, output=output, start=0.0, end=seconds, aspect="16:9", resolution="720p",
                edit_mode="full-length", keep_only_action=True, title_transcript=False, auto_sound_effect=False,
                export_metadata=metadata,
            )
        return metadata["full_length_summary"], video.probe_video(output).duration

    def test_the_hud_gives_shots_kills_deaths_and_time_out_of_play(self):
        events = hud_events(self.shooter, 0.0, 52.0, video.ffmpeg_executable())
        for start in self.BURSTS:
            self.assertTrue(any(start - 0.2 <= shot <= start + 1.2 for shot in events.shots), start)
        self.assertFalse(any(not any(start - 0.2 <= shot <= start + 1.3 for start in self.BURSTS) for shot in events.shots))
        self.assertEqual(len(events.kills), len(self.KILLS))
        self.assertTrue(all(abs(found - kill) <= 0.2 for found, kill in zip(events.kills, self.KILLS)))
        self.assertEqual(len(events.deaths), 1)
        self.assertAlmostEqual(events.deaths[0], 45.0, delta=0.2)
        self.assertEqual(len(events.hud_gone), 1)
        self.assertAlmostEqual(events.hud_gone[0][0], 47.0, delta=0.2)
        self.assertAlmostEqual(events.hud_gone[0][1], 50.0, delta=0.2)

    def test_a_video_without_the_counter_gives_no_shots(self):
        # A card alone, with no counter, is still a Black Ops II card.
        events = hud_events(self.plain, 0.0, 20.0, video.ffmpeg_executable())
        self.assertEqual(events.shots, ())
        self.assertEqual(len(events.kills), 1)

    def test_keeping_only_the_action_keeps_the_kills(self):
        summary, duration = self.export(self.shooter, 52.0)
        self.assertTrue(summary["keep_only_action"])
        self.assertEqual(summary["kills_found"], len(self.KILLS))
        self.assertEqual(summary["fights_found"], len(self.BURSTS))
        # Each kill: three and a half seconds before its card and one and a half after.
        self.assertAlmostEqual(duration, len(self.KILLS) * 5.0, delta=1.5)

    def test_pause_removal_leaves_fights_and_kills_alone(self):
        with patch.object(video, "transcribe_words", return_value=[]), \
                patch.object(video, "_speech_pause_cut_intervals", return_value=[]) as pauses:
            video.export_clip(
                source=self.shooter, output=self.root / "pauses.mp4", start=0.0, end=52.0, aspect="16:9",
                resolution="720p", edit_mode="full-length", remove_silence=True, title_transcript=False,
                auto_sound_effect=False,
            )
        protected = pauses.call_args.kwargs["fights"]
        events = hud_events(self.shooter, 0.0, 52.0, video.ffmpeg_executable())
        fights = fight_spans(events.shots, 52.0, hud_gone=events.hud_gone)
        kills = kill_spans(events.kills, events.shots, 52.0, events.hud_gone)
        for moment in (*fights, *kills):
            self.assertTrue(any(start <= moment[0] + 0.01 and moment[1] - 0.01 <= end for start, end in protected), moment)

    def test_without_kill_cards_the_fights_are_kept(self):
        summary, duration = self.export(self.no_cards, 52.0)
        self.assertEqual(summary["kills_found"], 0)
        self.assertEqual(summary["fights_found"], len(self.BURSTS))
        # Each fight: 2.5 s before the first shot, about a second of fire, 2 s after.
        self.assertAlmostEqual(duration, len(self.BURSTS) * 5.4, delta=2.5)


if __name__ == "__main__":
    unittest.main()
