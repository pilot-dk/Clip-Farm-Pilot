"""Swear words that are muted so uploads stay advertiser-friendly.

The list covers strong and moderate profanity and slurs, the language YouTube's
advertiser-friendly guidelines limit or remove ads for. Mild words such as
"damn" and "hell" are left alone: they do not affect monetisation, and muting
them would only make a video choppier.
"""
from __future__ import annotations

import re

# Words that count wherever they appear inside a token ("motherfucker", "bullshit").
_ANYWHERE = ("fuck", "shit", "bitch", "cunt")
# Ordinary words that happen to contain one of the above.
_HARMLESS = {"shitake", "shiitake", "matsushita", "scunthorpe"}
# Words that only count as the whole token, so "cocktail", "Dickens", and "class" stay.
_WHOLE_WORD = re.compile(
    r"""^(?:
        ass(?:hole|hat|wipe)s? | arseholes? |
        dick(?:s|head|heads|face|wad)? | cock(?:s|sucker|suckers|sucking)? |
        puss(?:y|ies) | bastards? | whores? | slut(?:s|ty)? | twats? | wankers? | bollocks |
        nigg(?:er|ers|a|as|az|ah|ahs) | fag(?:s|got|gots|gy)? | retard(?:s|ed)? |
        trann(?:y|ies) | kikes? | spics? | chinks? | dykes? | wetbacks? | gooks? | beaners?
    )$""",
    re.VERBOSE,
)
# How speech engines sometimes write a swear word themselves: "f***", "sh*t",
# "a**hole", "****". A sound tag such as "*heavy breathing*" is not one.
_MASKED = re.compile(r"^(?:[a-z]{1,2}\*{2,}[a-z]*|[a-z]+\*+[a-z]+\**|\*{3,})$")
# "Motherfucker" as a speech engine spells it when it is slurred or said in an
# accent it does not expect: "muthafucka", "Matherfica".
_SLURRED = re.compile(r"^m[a-z]{1,2}th[a-z]{1,3}f[a-z]{1,3}[ck][a-z]{0,3}$")
# Pairs a speech engine may split in two.
_SPLIT_PAIRS = {("ass", "hole"), ("ass", "holes"), ("arse", "hole"), ("arse", "holes")}


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z*]+", "", str(text).lower())


def is_profane(text: str) -> bool:
    """Whether one transcribed word is a swear word."""
    word = _normalized(text)
    if not word or word in _HARMLESS:
        return False
    return bool(
        _MASKED.match(word)
        or _WHOLE_WORD.match(word)
        or _SLURRED.match(word)
        or any(part in word for part in _ANYWHERE)
    )


def profane_indexes(texts: list[str]) -> list[int]:
    """Positions of the swear words in a run of transcribed words."""
    words = [_normalized(text) for text in texts]
    found = {index for index, text in enumerate(texts) if is_profane(text)}
    for index in range(len(words) - 1):
        if (words[index], words[index + 1]) in _SPLIT_PAIRS:
            found.update((index, index + 1))
    return sorted(found)


def masked(text: str) -> str:
    """A swear word as it may be shown on screen: its first letter, then asterisks."""
    letters = [index for index, character in enumerate(text) if character.isalpha()]
    if len(letters) < 2:
        return text
    hidden = set(letters[1:])
    return "".join("*" if index in hidden else character for index, character in enumerate(text))
