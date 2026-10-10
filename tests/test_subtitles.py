from pathlib import Path

from clipforage.models import Cue
from clipforage.phrase.subtitles import parse_srt, parse_subtitles, parse_vtt

FIXTURES = Path(__file__).parent / "fixtures" / "phrase"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_youtube_auto_vtt_rolling_duplicates_merged() -> None:
    cues = parse_vtt(read("youtube_auto.vtt"))
    assert cues == [
        Cue(start_s=1.92, end_s=10.23, text="[Music]"),
        Cue(start_s=10.24, end_s=12.84, text="good morning everyone welcome back"),
        Cue(start_s=12.84, end_s=14.639, text="today we're making the best coffee"),
        Cue(start_s=14.639, end_s=17.199, text="you & I have ever tasted"),
        Cue(start_s=17.199, end_s=19.439, text="this is not a drill"),
        Cue(start_s=19.439, end_s=22.349, text="okay let's go"),
    ]


def test_each_spoken_line_appears_once() -> None:
    texts = [c.text for c in parse_vtt(read("youtube_auto.vtt"))]
    assert len(texts) == len(set(texts))
    assert not any("<" in t or "&amp;" in t for t in texts)


def test_srt_parsing_tags_entities_multiline_and_empty() -> None:
    cues = parse_srt(read("film.srt"))
    assert cues == [
        Cue(start_s=5.0, end_s=7.5, text="They're coming to get you, Barbara."),
        Cue(start_s=8.1, end_s=9.9, text="Stop it! You're ignorant."),
        Cue(start_s=62.25, end_s=64.0, text="We'll be safe in the cellar."),
        Cue(start_s=3723.4, end_s=3726.0, text='Don\'t open that door "yet".'),
    ]


def test_srt_with_bom_crlf_and_no_index() -> None:
    text = (
        "﻿00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n00:00:03,000 --> 00:00:04,500\r\nWorld\r\n"
    )
    assert parse_srt(text) == [
        Cue(start_s=1.0, end_s=2.0, text="Hello"),
        Cue(start_s=3.0, end_s=4.5, text="World"),
    ]


def test_vtt_short_timestamps_and_note_blocks_ignored() -> None:
    text = (
        "WEBVTT\n\nNOTE this is a comment\n\nSTYLE\n::cue { color: red }\n\n"
        "cue-1\n01:02.500 --> 01:04.000 line:0\n<v Roger>Hi there</v>\n"
    )
    assert parse_vtt(text) == [Cue(start_s=62.5, end_s=64.0, text="Hi there")]


def test_identical_and_prefix_cues_merged() -> None:
    text = (
        "1\n00:00:01,000 --> 00:00:02,000\nnever gonna\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nnever gonna give\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nnever gonna give\n\n"
        "4\n00:00:04,000 --> 00:00:05,000\nnevertheless\n"
    )
    assert parse_srt(text) == [
        Cue(start_s=1.0, end_s=4.0, text="never gonna give"),
        Cue(start_s=4.0, end_s=5.0, text="nevertheless"),
    ]


def test_parse_subtitles_detects_format() -> None:
    vtt = read("youtube_auto.vtt")
    srt = read("film.srt")
    assert parse_subtitles(vtt) == parse_vtt(vtt)
    assert parse_subtitles(srt) == parse_srt(srt)
    assert parse_subtitles("﻿  WEBVTT\n\n00:01.000 --> 00:02.000\nx\n")[0].text == "x"
    assert parse_subtitles("00:00:01.000 --> 00:00:02.000\nx\n", "vtt")[0].start_s == 1.0
    assert parse_subtitles("") == []
    assert parse_subtitles("garbage\nno timing here\n") == []
