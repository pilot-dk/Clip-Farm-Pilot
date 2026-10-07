"""Keep only the action: cut a gameplay stream down to its kills, its fights and what the creator says.

A stream's best moments are its kills and its commentary. Running between fights,
deaths and killcams, waiting and menus go. All of it is read from the game's own
HUD rather than from the sound, which cannot tell fights apart: in a team match
someone is shooting somewhere nearly all the time, and a suppressed weapon -- one
Black Ops stream measured used one -- makes the creator's own shots too soft for
any sound model to call gunfire. The HUD is exact: the ammo counter drops with every
shot and is gone while the player is dead, in a killcam or in a menu, and Black
Ops II names every kill on a card.
"""
from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .captions import CaptionWord

# Call of Duty style shooters draw the ammo bottom right, such as "38/89": the
# magazine (38) changes with every shot, while the reserve (/89) only changes on a
# reload, a weapon swap or a pickup. The counter is drawn over the game, so scenery
# behind it changes both halves at once, and a shot is where the magazine changes
# while the reserve does not. Measured on a 1080p Black Ops II stream, scaled with
# the frame for other sizes.
SHOT_FPS = 10
_COUNTER_BOX = (0.80729, 0.81944, 0.09375, 0.05556)  # x, y, width and height as shares of the frame.
_COUNTER_SIZE = (180, 60)
_MAGAZINE_COLUMNS = slice(0, 90)
_RESERVE_COLUMNS = slice(95, 180)
_DIGIT_WHITE = 215        # Grey level the white digits reach.
_SHOT_CHANGE = 0.04       # Share of the magazine digits that a new number changes.
_RESERVE_STILL = 0.01     # Share of the reserve that may change while the magazine does.
_SETTLED_CHANGE = 0.02    # Below this the magazine is showing one steady number.
# A real counter keeps its reserve digits on screen: in the Black Ops streams they
# covered about 5% of their box and changed 0.4% of it a frame, while a video
# without one had none there or 2% changing. Fewer shot bursts than this mean no
# counter was found.
_RESERVE_WHITE = (0.02, 0.30)
_RESERVE_QUIET = 0.01
_FEWEST_BURSTS = 5
# With neither half of the counter showing any white for a second, the player is
# out of live play: dead, watching a killcam, in a menu or between matches.
_HUD_GONE_WHITE = 0.01
_HUD_GONE_FRAMES = 10

# Black Ops II puts a card at the bottom of the screen for about two seconds after
# every kill: "Killed" above the victim's calling card, and "Killed By" above the
# killer's when the player dies. The label is always drawn the same way, so it is
# found by matching its picture, which holds over bright or dark scenery where a
# brightness threshold does not. On two Black Ops II streams every card was found
# and nothing else matched: a card scored 0.7 to 1 and anything else under 0.5,
# and in four other videos nothing scored even 0.4.
_CARD_BOX = (760 / 1920, 860 / 1080, 154 / 1920, 65 / 1080)
_CARD_SIZE = (154, 65)
_KILLED_AT = (10, 16)     # Row and column of the "Killed" picture in the card box.
_BY_AT = (10, 78)         # And of "By", which follows it on a death card.
_CARD_SHIFT = 3           # Pixels a label may sit off its measured place.
_LABEL_MATCH = 0.6
_BY_MATCH = 0.45          # "By" follows: a death card.
_NO_BY = 0.3              # Nothing like "By" follows: a kill card. Frames between count for neither.
_CARD_GAP = 6             # Frames a card may drop out, in a flash or over white scenery.
_CARD_FRAMES = 3          # Frames that make a card.
# Grey levels 0 to 9, from the cards of a 1080p stream.
_KILLED_PICTURE = (
    "11111111111111111111111111111111111111111111111111111111111111",
    "11111111111111111111111111111111111111111111111111111111111111",
    "11111110111111111111111100111111111111111111111111111000111111",
    "11111122111121111111111121101122111111111111111111111111111111",
    "11112244212243211111111143101244111111111111111111111244111111",
    "11112366322465321111111276101366111111111111111111112356111111",
    "11111477334675210122101487201488111111111111111111111477111111",
    "11111488435775211243101497201588111111111111111111111588111111",
    "11111488436874112355101497201588111111111111111111111588111111",
    "11111488446764112466101497201588111111111111111111111588111111",
    "11111588667742101244101497201588101100000000000111012588111111",
    "11111588778631011244101497201588101122221221000122233688111111",
    "11111588788620011365101497201588101155555555000134566798111111",
    "11111588887510012477101497201598101288777887000167787898111111",
    "11111588887510011588101497201588101398766898101388766798111111",
    "11111488987510011598101497201588101498655798101498544698111111",
    "11111488887510011598101497201588101498654698101498532698111111",
    "11111488887521011598101597201588101498655798101498421588111111",
    "11111588677731111598101597201588101498767787101498421588111111",
    "11111588567852111598101497201588101498766665101498411598111111",
    "11111588557863111598101497201588101498543444101498411598111111",
    "11111588446874211588101497201588101498432355101498411598111111",
    "11111588435775211587101497201588101488533576101488533688111111",
    "11112588423686421587101497201588101488655787101488756788111111",
    "11111478322586421477101487201477101388767787101378877888111111",
    "11110356211355311355101365101355101355555555101345555565111111",
    "11110122101122110122101222100122101122222222101111221222111111",
)
_BY_PICTURE = (
    "111111111111111111111111111111",
    "111111111111111111111111111111",
    "111111000111111111111111111111",
    "111111111111111111111111111111",
    "111244444443101111111111111111",
    "111478777776211111111111111111",
    "111589877888201111111111111111",
    "111599655698201111111111111111",
    "111598522598201111111111111111",
    "111598411488201111111111111111",
    "111598411488200000000001111111",
    "111598411498201111011111111111",
    "111598422598203443113442111111",
    "111598644697204775225764111111",
    "111599877886104786236863101111",
    "111599888875102687337863101111",
    "111599755786102588447852101111",
    "111599523587101588547842001111",
    "111598411488201478668731001111",
    "111598411498201368778620001111",
    "111598411498201368888610001111",
    "111598411498201258988510001111",
    "111598522598201147997410001111",
    "111599755798201136986310001111",
    "111589877887200025985310001111",
    "111356555555100025885210001111",
    "110122111122110126874111101111",
    "110111000011111136863111111111",
    "111111111111111147862111111111",
    "111111111111111247752011111111",
)

# A fight keeps the run-up before the first shot and the kill landing after the last.
_FIGHT_GAP = 4.0
_FIGHT_LEAD = 2.5
_FIGHT_TAIL = 2.0
# A hard cut, where nearly the whole picture changes between frames, ends a fight's
# tail early: it is the death camera, a killcam or the end of the match.
_SCENE_CUT_MOTION = 0.85
# A kill keeps the approach and the shots before it and its card after it; a longer
# fight leading into it keeps its start, within reason. Shots landed within two
# seconds of the card on one Black Ops II stream, nearly all within one.
_KILL_LEAD = 3.5
_KILL_TAIL = 1.5
_KILL_SHOT_GAP = 2.0
_SHOT_LEAD = 1.5
_LONGEST_KILL_LEAD = 8.0
# What the creator says is kept with a breath either side; a phrase must have
# some substance to stand on its own.
_TALK_LEAD = 0.35
_TALK_TAIL = 0.45
_PHRASE_GAP = 1.0
_PHRASE_WORDS = 3
_PHRASE_SECONDS = 0.8
# Kept stretches closer than this are joined, and shorter ones dropped.
_JOIN_GAP = 1.5
_SHORTEST_KEPT = 1.2

_HUD_CHUNK = 600          # Frames read and measured at a time: a minute of video.


@dataclass(frozen=True)
class GameEvents:
    """What the HUD showed, in seconds from the start of the selection."""

    shots: tuple[float, ...] = ()
    kills: tuple[float, ...] = ()
    deaths: tuple[float, ...] = ()
    # Stretches without the ammo counter, found only when the HUD was recognised.
    hud_gone: tuple[tuple[float, float], ...] = ()


_EVENTS_CACHE: dict[tuple[object, ...], GameEvents] = {}
_EVENTS_CACHE_LOCK = threading.Lock()


def _picture(rows: tuple[str, ...]) -> np.ndarray:
    picture = np.array([[float(level) for level in row] for row in rows], dtype=np.float32)
    picture -= picture.mean()
    return picture / float(np.sqrt((picture * picture).sum()))


_KILLED = _picture(_KILLED_PICTURE)
_BY = _picture(_BY_PICTURE)
_SHIFTS = [(dy, dx) for dy in range(-_CARD_SHIFT, _CARD_SHIFT + 1) for dx in range(-_CARD_SHIFT, _CARD_SHIFT + 1)]


def _correlations(frames: np.ndarray, picture: np.ndarray, top: int, left: int) -> np.ndarray:
    """Correlation of the picture with each frame's patch at each shift: frames by shifts."""
    height, width = picture.shape
    scores = np.empty((len(frames), len(_SHIFTS)), dtype=np.float32)
    for column, (dy, dx) in enumerate(_SHIFTS):
        patch = frames[:, top + dy:top + dy + height, left + dx:left + dx + width]
        patch = patch - patch.mean(axis=(1, 2), keepdims=True)
        energy = np.sqrt((patch * patch).sum(axis=(1, 2))) + 1e-3
        scores[:, column] = (patch * picture).sum(axis=(1, 2)) / energy
    return scores


def card_matches(cards: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """How closely each frame's card box shows "Killed", and "By" after it where it does."""
    frames = np.asarray(cards, dtype=np.float32)
    if not len(frames):
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    scores = _correlations(frames, _KILLED, *_KILLED_AT)
    best = np.argmax(scores, axis=1)
    killed = scores[np.arange(len(frames)), best]
    by = np.full(len(frames), -1.0, dtype=np.float32)
    height, width = _BY.shape
    for index in np.flatnonzero(killed >= _LABEL_MATCH):
        dy, dx = _SHIFTS[best[index]]
        top, left = _BY_AT[0] + dy, _BY_AT[1] + dx
        patch = frames[index, top:top + height, left:left + width]
        patch = patch - patch.mean()
        by[index] = float((patch * _BY).sum() / (np.sqrt((patch * patch).sum()) + 1e-3))
    return killed, by


def card_events(
    killed: np.ndarray, by: np.ndarray, frames_per_second: float = SHOT_FPS,
) -> tuple[list[float], list[float]]:
    """Seconds at which a kill card and a death card came up."""
    labelled = np.asarray(killed) >= _LABEL_MATCH
    death = labelled & (np.asarray(by) >= _BY_MATCH)
    kill = labelled & (np.asarray(by) < _NO_BY)

    def first_frames(mask: np.ndarray) -> list[float]:
        runs: list[list[int]] = []
        for index in np.flatnonzero(mask):
            if runs and index - runs[-1][1] <= _CARD_GAP:
                runs[-1][1] = int(index)
                runs[-1][2] += 1
            else:
                runs.append([int(index), int(index), 1])
        return [round(first / frames_per_second, 2) for first, _, count in runs if count >= _CARD_FRAMES]

    return first_frames(kill), first_frames(death)


def hud_readings(source: Path, start: float, end: float, ffmpeg: str) -> dict[str, np.ndarray]:
    """The ammo counter and the kill card box ten times a second, from one decode.

    Both boxes are cut from each frame and stacked side by side, and the video is
    read a minute at a time, so a long stream never has to be held in memory.
    """
    counter_width, counter_height = _COUNTER_SIZE
    card_width, card_height = _CARD_SIZE
    height = max(counter_height, card_height)
    width = counter_width + card_width

    def box(x: float, y: float, w: float, h: float, size: tuple[int, int]) -> str:
        return f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={size[0]}:{size[1]}:flags=area,format=gray"

    graph = (
        f"[0:v]fps={SHOT_FPS},split=2[counter][card];"
        f"[counter]{box(*_COUNTER_BOX, _COUNTER_SIZE)},pad={counter_width}:{height}[left];"
        f"[card]{box(*_CARD_BOX, _CARD_SIZE)},pad={card_width}:{height}[right];"
        f"[left][right]hstack=inputs=2[hud]"
    )
    process = subprocess.Popen(
        [
            ffmpeg, "-v", "error", "-skip_loop_filter", "all",
            "-ss", f"{max(0.0, float(start)):.3f}", "-t", f"{max(0.1, float(end) - float(start)):.3f}",
            "-i", str(source), "-an", "-filter_complex", graph, "-map", "[hud]",
            "-f", "rawvideo", "pipe:1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    frame_bytes = width * height
    parts: dict[str, list[np.ndarray]] = {
        name: [] for name in ("magazine_change", "reserve_change", "magazine_white", "reserve_white", "killed", "by")
    }
    previous: np.ndarray | None = None
    try:
        while True:
            data = bytearray()
            while len(data) < frame_bytes * _HUD_CHUNK:
                more = process.stdout.read(frame_bytes * _HUD_CHUNK - len(data))
                if not more:
                    break
                data += more
            count = len(data) // frame_bytes
            if not count:
                break
            frames = np.frombuffer(bytes(data[:count * frame_bytes]), dtype=np.uint8).reshape(count, height, width)
            white = frames[:, :counter_height, :counter_width] > _DIGIT_WHITE
            magazine, reserve = white[:, :, _MAGAZINE_COLUMNS], white[:, :, _RESERVE_COLUMNS]
            before = np.concatenate([white[:1] if previous is None else previous, white[:-1]])
            parts["magazine_change"].append(np.mean(magazine ^ before[:, :, _MAGAZINE_COLUMNS], axis=(1, 2)))
            parts["reserve_change"].append(np.mean(reserve ^ before[:, :, _RESERVE_COLUMNS], axis=(1, 2)))
            parts["magazine_white"].append(np.mean(magazine, axis=(1, 2)))
            parts["reserve_white"].append(np.mean(reserve, axis=(1, 2)))
            killed, by = card_matches(frames[:, :card_height, counter_width:])
            parts["killed"].append(killed)
            parts["by"].append(by)
            previous = white[-1:]
            if count < _HUD_CHUNK:
                break
    finally:
        process.kill()
        process.stdout.close()
        process.wait()
    return {
        name: np.concatenate(values).astype(np.float32) if values else np.zeros(0, dtype=np.float32)
        for name, values in parts.items()
    }


def shots_from_counter(
    magazine_change: np.ndarray,
    reserve_change: np.ndarray,
    magazine_white: np.ndarray,
    reserve_white: np.ndarray,
    frames_per_second: float = SHOT_FPS,
) -> list[float]:
    """Seconds at which the player fired, or nothing when the video shows no such counter.

    Automatic fire changes the magazine on frame after frame. A single shot -- a
    sniper rifle or a shotgun -- changes it once, between two steady numbers, and
    the digits stay white: the low-ammo warning turns them red, which only looks
    like a change.
    """
    count = int(magazine_change.size)
    if count < 3:
        return []
    candidate = (magazine_change > _SHOT_CHANGE) & (reserve_change < _RESERVE_STILL)
    neighbours = np.convolve(candidate.astype(np.int8), np.ones(5, dtype=np.int8), mode="same")
    burst = candidate & (neighbours >= 2)
    single = np.zeros(count, dtype=bool)
    for index in np.flatnonzero(candidate & ~burst):
        before = magazine_change[max(0, index - 3):index]
        after = magazine_change[index + 1:index + 4]
        if before.size < 3 or after.size < 3 or before.max() >= _SETTLED_CHANGE or after.max() >= _SETTLED_CHANGE:
            continue
        was, now = float(magazine_white[index - 1]), float(magazine_white[min(count - 1, index + 1)])
        if min(was, now) >= 0.4 * max(was, now) > 0:
            single[index] = True
    shots = burst | single
    bursts = int(np.count_nonzero(np.diff(np.concatenate([[0], burst.astype(np.int8)])) == 1))
    looks_like_counter = (
        _RESERVE_WHITE[0] <= float(np.median(reserve_white)) <= _RESERVE_WHITE[1]
        and float(np.median(reserve_change)) < _RESERVE_QUIET
    )
    if not looks_like_counter or bursts < _FEWEST_BURSTS:
        return []
    return [round(float(index) / frames_per_second, 2) for index in np.flatnonzero(shots)]


def hud_gone_stretches(
    magazine_white: np.ndarray, reserve_white: np.ndarray, frames_per_second: float = SHOT_FPS,
) -> list[tuple[float, float]]:
    """Stretches of a second or more with no ammo counter on screen."""
    gone = np.maximum(np.asarray(magazine_white), np.asarray(reserve_white)) < _HUD_GONE_WHITE
    edges = np.diff(np.concatenate([[0], gone.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return [
        (round(float(first) / frames_per_second, 2), round(float(last) / frames_per_second, 2))
        for first, last in zip(starts, ends)
        if last - first >= _HUD_GONE_FRAMES
    ]


def game_events(readings: dict[str, np.ndarray]) -> GameEvents:
    """Shots, kills, deaths and the stretches out of live play, from the HUD readings."""
    counter = [readings[name] for name in ("magazine_change", "reserve_change", "magazine_white", "reserve_white")]
    shots = shots_from_counter(*counter)
    kills, deaths = card_events(readings["killed"], readings["by"])
    # The counter's absence only means something in a game known to draw one.
    gone = hud_gone_stretches(counter[2], counter[3]) if shots or kills or deaths else []
    return GameEvents(tuple(shots), tuple(kills), tuple(deaths), tuple(gone))


def hud_events(source: Path, start: float, end: float, ffmpeg: str) -> GameEvents:
    """What the game's HUD showed between `start` and `end`, in seconds from `start`."""
    try:
        resolved = Path(source).resolve()
        stat = resolved.stat()
        key = (str(resolved), stat.st_size, stat.st_mtime_ns, round(float(start), 3), round(float(end), 3))
    except OSError:
        key = None
    if key is not None:
        with _EVENTS_CACHE_LOCK:
            if key in _EVENTS_CACHE:
                return _EVENTS_CACHE[key]
    events = game_events(hud_readings(source, start, end, ffmpeg))
    if key is not None:
        with _EVENTS_CACHE_LOCK:
            _EVENTS_CACHE[key] = events
            while len(_EVENTS_CACHE) > 6:
                _EVENTS_CACHE.pop(next(iter(_EVENTS_CACHE)))
    return events


# The game's own voice lines are heard as speech too: "UAV inbound", "Concussion
# out!", "Kill confirmed", "We've lost the advantage", "Get out there, you know what
# to do". Whisper spells them in many ways ("You av inbound", "cushing out",
# "Hostel you, Evie, above", "The advice hostile counter UAP is online").
_CALLOUT = re.compile(
    r"\b(?:u\.?a\.?v|u\.?a\.?p|a\.?v|avi|evie|inbound|hostile|hostel|hostyle|how style|advised|concussion|"
    r"cushing|touching|grenade|sticky|frag|flashbang|reload\w*|tac|standby|stand by|deathmatch|death match|"
    r"advantage|mission|tasking|priority|confirm|confirmed|care package|the lead|lost control|taking control|"
    r"transitioning|jeopardy|executed|orders|cover me|gun down|get moving|speed and aggression|pulled forward|"
    r"pick it up|down to this|this is it|i'm dry|got you|move up|move it|move in|take 'em out|threat|tango|"
    r"on your feet|on your face|enemy down|friendly|deployed|drone|hunter.?killer|radar|rc.?xd|rcx|"
    r"tactical insertion|get out there|you know what to do|get up and move|get down|don't stop until|all dead|"
    r"pressing the fight|we're ahead)\b",
    re.IGNORECASE,
)


def is_game_callout(text: str) -> bool:
    """Whether a heard phrase is one of the game's voice lines rather than the creator talking.

    It is when callouts make up much of it: one in every three words, or most of
    its words when the callouts are whole lines such as "you know what to do".
    """
    words = re.findall(r"[\w'.]+", text)
    if not words:
        return False
    callouts = _CALLOUT.findall(text)
    covered = sum(len(re.findall(r"[\w'.]+", callout)) for callout in callouts)
    return len(callouts) >= max(1, len(words) // 3) or covered >= 0.6 * len(words)


def talking_spans(words: list[CaptionWord] | tuple[CaptionWord, ...], duration: float) -> list[tuple[float, float]]:
    """Where the creator says something worth keeping, from the words heard (sound tags already out)."""
    phrases: list[list[CaptionWord]] = []
    for word in words:
        if phrases and word.start - phrases[-1][-1].end <= _PHRASE_GAP:
            phrases[-1].append(word)
        else:
            phrases.append([word])
    spans: list[tuple[float, float]] = []
    for phrase in phrases:
        text = " ".join(word.text for word in phrase)
        long_enough = len(phrase) >= _PHRASE_WORDS or phrase[-1].end - phrase[0].start >= _PHRASE_SECONDS
        if long_enough and not is_game_callout(text):
            spans.append((max(0.0, phrase[0].start - _TALK_LEAD), min(duration, phrase[-1].end + _TALK_TAIL)))
    return spans


def _in_live_play(
    start: float, end: float, first: float, last: float, hud_gone: tuple[tuple[float, float], ...] | list,
) -> tuple[float, float]:
    """Trim a stretch to the live play around its moments, `first` to `last`.

    It starts no earlier than the HUD's return before `first` -- a respawn -- and
    ends no later than its disappearance after `last`: a death, a killcam or the
    end of the match.
    """
    for gone_start, gone_end in hud_gone:
        if gone_end <= first:
            start = max(start, gone_end)
        elif gone_start >= last:
            end = min(end, gone_start)
    return start, end


def fight_spans(
    shots: list[float] | tuple[float, ...],
    duration: float,
    motion: np.ndarray | None = None,
    motion_fps: float = 4.0,
    hud_gone: tuple[tuple[float, float], ...] | list = (),
) -> list[tuple[float, float]]:
    """Each run of shots with its run-up and its landing, ended early by a hard cut."""
    fights: list[list[float]] = []
    for shot in sorted(shots):
        if fights and shot - fights[-1][1] <= _FIGHT_GAP:
            fights[-1][1] = shot
        else:
            fights.append([shot, shot])
    spans: list[tuple[float, float]] = []
    for first, last in fights:
        end = min(duration, last + _FIGHT_TAIL)
        if motion is not None and motion.size:
            frames = range(int((last + 0.25) * motion_fps), min(motion.size, int(end * motion_fps) + 1))
            cut = next((index for index in frames if motion[index] >= _SCENE_CUT_MOTION), None)
            if cut is not None:
                end = max(last + 0.25, cut / motion_fps)
        spans.append(_in_live_play(max(0.0, first - _FIGHT_LEAD), end, first, last, hud_gone))
    return spans


def kill_spans(
    kills: list[float] | tuple[float, ...],
    shots: list[float] | tuple[float, ...],
    duration: float,
    hud_gone: tuple[tuple[float, float], ...] | list = (),
) -> list[tuple[float, float]]:
    """Each kill with the approach and the shots that led to it, and its card after."""
    ordered = sorted(shots)
    spans: list[tuple[float, float]] = []
    for kill in sorted(kills):
        start = kill - _KILL_LEAD
        before = [shot for shot in ordered if shot <= kill + 0.3]
        if before and kill - before[-1] <= _KILL_SHOT_GAP:
            first = before[-1]
            for shot in reversed(before[:-1]):
                if first - shot > _FIGHT_GAP:
                    break
                first = shot
            start = min(start, first - _SHOT_LEAD)
        start = max(0.0, start, kill - _LONGEST_KILL_LEAD)
        end = min(duration, kill + _KILL_TAIL)
        spans.append(_in_live_play(start, end, kill, kill, hud_gone))
    return spans


def action_keep_spans(
    moments: list[tuple[float, float]], talking: list[tuple[float, float]], duration: float,
) -> list[tuple[float, float]]:
    """The stretches a highlights edit keeps: its moments and the talking, joined and tidied."""
    joined: list[list[float]] = []
    for start, end in sorted([*moments, *talking]):
        if joined and start <= joined[-1][1] + _JOIN_GAP:
            joined[-1][1] = max(joined[-1][1], end)
        else:
            joined.append([start, end])
    return [
        (round(max(0.0, start), 3), round(min(duration, end), 3))
        for start, end in joined
        if min(duration, end) - max(0.0, start) >= _SHORTEST_KEPT
    ]
