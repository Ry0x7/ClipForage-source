"""Offline fuzzy recall: alias data validity and description -> meme name expectations."""

import json
from importlib import resources

import pytest

from clipforage import recall
from clipforage.models import Kind


def _raw() -> list[dict[str, object]]:
    text = (
        resources.files("clipforage.providers.data")
        .joinpath(recall.ALIASES_FILE)
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def test_alias_data_is_valid() -> None:
    raw = _raw()
    assert len(raw) >= 80
    names = [str(item["name"]) for item in raw]
    assert len(names) == len(set(names)), "duplicate names"
    normalized = [recall.normalize(n) for n in names]
    assert len(normalized) == len(set(normalized)), "names equal after normalisation"
    valid_kinds = {k.value for k in Kind}
    for item in raw:
        assert set(item) <= {"name", "aliases", "kinds", "tags"}, item
        assert str(item["name"]).strip()
        aliases = item["aliases"]
        assert isinstance(aliases, list) and aliases, item["name"]
        assert all(isinstance(a, str) and a.strip() for a in aliases), item["name"]
        kinds = item.get("kinds", [])
        assert isinstance(kinds, list)
        assert set(kinds) <= valid_kinds, item["name"]
        assert isinstance(item.get("tags", []), list)
    assert len(recall.load_entries()) == len(raw)


@pytest.mark.parametrize(
    ("description", "kind", "expected"),
    [
        ("cat crying at the dinner table", None, "woman yelling at cat"),
        ("dog in burning room", None, "this is fine"),
        ("undertale sans", None, "megalovania"),
        ("guy looking back at another girl", None, "distracted boyfriend"),
        ("guy tapping head", None, "roll safe"),
        ("bass boom sound effect", Kind.AUDIO_SFX, "vine boom"),
        ("dancing pallbearers", None, "coffin dance"),
        ("old man smiling through pain", None, "hide the pain harold"),
        ("sad cat with tears", Kind.VIDEO, "crying cat"),
        ("pikachu shocked", None, "surprised pikachu"),
        ("windows error", Kind.AUDIO_SFX, "windows xp error sound"),
        ("undertale bgm", Kind.AUDIO_MUSIC, "undertale soundtrack"),
        ("girl smiling in front of burning house", Kind.IMAGE, "disaster girl"),
    ],
)
def test_description_expands_to_name(description: str, kind: Kind | None, expected: str) -> None:
    assert expected in recall.expand_query(description, kind)


def test_best_match_is_first() -> None:
    assert recall.expand_query("dog drinking coffee while house is on fire")[0] == "this is fine"


def test_no_self_expansion() -> None:
    for query in ("crying cat", "Crying Cat!", "crying cat meme", "megalovania", "this is fine"):
        names = recall.expand_query(query)
        assert all(recall.normalize(n) != recall.normalize(query) for n in names), names
    assert "crying cat" not in recall.expand_query("crying cat meme")


def test_unrelated_or_empty_queries_expand_to_nothing() -> None:
    assert recall.expand_query("rain on window") == []
    assert recall.expand_query("green screen") == []  # stopwords only
    assert recall.expand_query("") == []
    assert recall.expand_query("!!!") == []


def test_max_expansions_and_order_are_deterministic() -> None:
    first = recall.expand_query("cat", max_expansions=3)
    assert len(first) == 3
    assert all(recall.expand_query("cat", max_expansions=3) == first for _ in range(5))
    assert recall.expand_query("cat", max_expansions=1) == first[:1]
    assert recall.expand_query("cat", max_expansions=0) == []


def test_kind_filter_skips_incompatible_entries() -> None:
    # megalovania is music; image searches never get it, sfx searches do (audio kinds mix).
    assert "megalovania" not in recall.expand_query("undertale sans", Kind.IMAGE)
    assert "megalovania" in recall.expand_query("undertale sans", Kind.AUDIO_SFX)
    # "this is fine" has no audio kind.
    assert "this is fine" not in recall.expand_query("dog in burning room", Kind.AUDIO_MUSIC)


def test_related_audio_kind() -> None:
    assert recall.related_audio_kind(Kind.AUDIO_MUSIC) == Kind.AUDIO_SFX
    assert recall.related_audio_kind(Kind.AUDIO_SFX) == Kind.AUDIO_MUSIC
    assert recall.related_audio_kind(Kind.VIDEO) is None
    assert recall.related_audio_kind(None) is None


def test_tokenize_drops_stopwords_and_folds_plurals() -> None:
    assert recall.tokenize("The Cats with a Sound Effect!") == ("cat",)
    assert recall.normalize("They don't KNOW") == "they dont know"


@pytest.mark.parametrize(
    ("query", "kind", "expected"),
    [
        ("undertale bgm", None, "undertale"),
        ("Undertale OST", Kind.AUDIO_MUSIC, "Undertale"),
        ("megalovania remix", Kind.AUDIO_MUSIC, "megalovania remix"),  # remix is kept
        ("vine boom sound effect", Kind.AUDIO_SFX, "vine boom"),
        ("mario theme song instrumental", Kind.AUDIO_MUSIC, "mario"),
        ("bruh meme sfx", Kind.AUDIO_SFX, "bruh"),
        ("bruh green screen", Kind.AUDIO_SFX, "bruh"),
        ("bruh green-screen template", Kind.AUDIO_SFX, "bruh"),
        ("crying cat meme", Kind.VIDEO, "crying cat meme"),  # meme words only go for audio
        ("crying cat green screen", None, "crying cat green screen"),
        ("  zelda   music  track ", None, "zelda"),
        ("bgm", Kind.AUDIO_MUSIC, "bgm"),  # nothing left: original query back
        ("  sound   effect ", None, "sound effect"),
        ("tetris music!", None, "tetris"),
    ],
)
def test_core_query(query: str, kind: Kind | None, expected: str) -> None:
    assert recall.core_query(query, kind) == expected
