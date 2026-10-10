"""Parse WebVTT and SubRip subtitles into ``Cue`` lists.

Tags (``<c>``, ``<i>``, inline ``<00:00:01.000>`` timestamps) and HTML entities are stripped,
multi-line cue text is joined with spaces and empty cues are dropped. YouTube auto-captions
repeat each line in the next cue ("rolling" captions); those repeats are removed so every
spoken line appears once, with the earliest start and the latest end.
"""

import html
import re
from typing import Literal

from clipforage.models import Cue

SubtitleFormat = Literal["vtt", "srt"]

_TIMING_RE = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")


def _seconds(stamp: str) -> float:
    """``[HH:]MM:SS.mmm`` (``,`` or ``.`` before the millis) -> seconds."""
    hms, _, frac = stamp.replace(",", ".").partition(".")
    seconds = 0.0
    for part in hms.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds + (float(f"0.{frac}") if frac else 0.0)


def _clean(line: str) -> str:
    text = html.unescape(_TAG_RE.sub("", line))
    return _SPACE_RE.sub(" ", text).strip()


def _raw_cues(text: str) -> list[tuple[float, float, list[str]]]:
    """Every ``-->`` timing line and its text as ``(start, end, cleaned non-empty lines)``.

    A cue's text runs until an empty line or the next timing line. Whitespace-only lines do
    not end a cue (YouTube auto-captions start cues with a ``" "`` line).
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, float, list[str]]] = []
    current: tuple[float, float, list[str]] | None = None
    for line in text.split("\n"):
        match = _TIMING_RE.match(line)
        if match is not None:
            if current is not None:
                # No blank line before this timing line: drop a dangling SRT cue number.
                if current[2] and current[2][-1].isdigit():
                    current[2].pop()
                cues.append(current)
            current = (_seconds(match["start"]), _seconds(match["end"]), [])
        elif current is not None:
            if line == "":
                cues.append(current)
                current = None
            elif cleaned := _clean(line):
                current[2].append(cleaned)
    if current is not None:
        cues.append(current)
    return cues


def _dedupe(raw: list[tuple[float, float, list[str]]]) -> list[Cue]:
    """Drop rolling repeats, join lines, then merge identical / prefix-of-next neighbours."""
    cues: list[Cue] = []
    previous: list[str] = []
    for start, end, lines in raw:
        fresh = list(lines)
        # Rolling captions start with the line(s) the previous cue already showed.
        while fresh and fresh[0] in previous:
            fresh.pop(0)
        previous = lines
        text = " ".join(fresh).strip()
        if text:
            cues.append(Cue(start_s=start, end_s=end, text=text))
        elif lines and cues and cues[-1].text.endswith(" ".join(lines)) and end > cues[-1].end_s:
            # A pure repeat of what is on screen: it only extends the last cue.
            cues[-1] = cues[-1].model_copy(update={"end_s": end})
    merged: list[Cue] = []
    for cue in cues:
        if merged:
            last = merged[-1]
            if cue.text == last.text or cue.text.startswith(last.text + " "):
                merged[-1] = Cue(
                    start_s=min(last.start_s, cue.start_s),
                    end_s=max(last.end_s, cue.end_s),
                    text=cue.text,
                )
                continue
        merged.append(cue)
    return merged


def parse_vtt(text: str) -> list[Cue]:
    """Parse a WebVTT document (header, NOTE/STYLE blocks and cue settings are ignored)."""
    return _dedupe(_raw_cues(text))


def parse_srt(text: str) -> list[Cue]:
    """Parse a SubRip document (cue numbers are optional)."""
    return _dedupe(_raw_cues(text))


def parse_subtitles(text: str, fmt_hint: SubtitleFormat | str | None = None) -> list[Cue]:
    """Parse VTT or SRT; a ``WEBVTT`` header (or ``fmt_hint="vtt"``) selects VTT."""
    if (fmt_hint or "").lower() == "vtt" or text.lstrip("﻿ \t\r\n").startswith("WEBVTT"):
        return parse_vtt(text)
    return parse_srt(text)
