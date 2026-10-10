"""Offline fuzzy recall: map a description ("dog in burning room") to known meme names.

Entries live in ``providers/data/meme_aliases.json``: ``name`` (the canonical search phrase),
``aliases`` (how people describe it without knowing the name), optional ``kinds`` and ``tags``.
Scoring is IDF-weighted token coverage of the query by an entry's name + aliases + tags, plus a
bonus when a whole alias phrase appears in the query (or the query inside an alias). Pure,
deterministic and network-free; the data is loaded once.
"""

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from clipforage.models import Kind

ALIASES_FILE = "meme_aliases.json"
AUDIO_KINDS: frozenset[Kind] = frozenset({Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})
MIN_COVERAGE = 0.6
"""Minimum IDF-weighted share of the query's tokens an entry must match to be suggested."""
PHRASE_BONUS = 0.3
"""Added to the coverage (for ranking) when a whole alias phrase matches."""
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "of", "with", "and", "or", "in", "on", "at", "to", "for", "from",
        "by", "is", "it", "its", "this", "that", "my", "me", "i", "im", "some", "very", "just",
        "meme", "memes", "sound", "sounds", "effect", "effects", "sfx", "fx", "funny", "video",
        "clip", "template", "green", "screen", "greenscreen", "hd", "4k", "original", "full",
        "version", "loop", "background", "transparent", "png", "gif", "song", "music", "bgm",
        "ost", "theme", "audio", "free", "download", "mp3", "mp4",
    }
)  # fmt: skip
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class AliasEntry:
    """One known meme / template / sound with the ways people describe it."""

    name: str
    aliases: tuple[str, ...]
    kinds: frozenset[Kind] | None
    tags: tuple[str, ...]
    tokens: frozenset[str]
    phrases: tuple[tuple[str, ...], ...]
    """Content-token sequences of the name and every alias."""


def _stem(token: str) -> str:
    """Very light plural folding so "cats" matches "cat" (applied to queries and data alike)."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize(text: str) -> str:
    """Lowercase, drop apostrophes, turn other punctuation into spaces, collapse whitespace."""
    return " ".join(_TOKEN_RE.findall(text.lower().replace("'", "").replace("’", "")))


def tokenize(text: str) -> tuple[str, ...]:
    """Content tokens of ``text``: normalised, stopwords dropped, lightly stemmed, in order."""
    return tuple(_stem(t) for t in normalize(text).split() if t not in STOPWORDS)


@lru_cache(maxsize=1)
def load_entries() -> tuple[AliasEntry, ...]:
    """Parse the shipped alias data once."""
    raw = json.loads(
        resources.files("clipforage.providers.data").joinpath(ALIASES_FILE).read_text("utf-8")
    )
    entries: list[AliasEntry] = []
    for item in raw:
        name = str(item["name"])
        aliases = tuple(str(a) for a in item.get("aliases", []))
        tags = tuple(str(t) for t in item.get("tags", []))
        kinds = frozenset(Kind(k) for k in item["kinds"]) if item.get("kinds") else None
        phrases = tuple(p for p in (tokenize(s) for s in (name, *aliases)) if p)
        tokens = frozenset(t for p in phrases for t in p) | frozenset(
            t for tag in tags for t in tokenize(tag)
        )
        entries.append(AliasEntry(name, aliases, kinds, tags, tokens, phrases))
    return tuple(entries)


@lru_cache(maxsize=1)
def _idf() -> dict[str, float]:
    entries = load_entries()
    df: dict[str, int] = {}
    for entry in entries:
        for token in entry.tokens:
            df[token] = df.get(token, 0) + 1
    n = len(entries)
    return {token: math.log(1 + n / count) for token, count in df.items()}


def _kind_ok(entry: AliasEntry, kind: Kind | None) -> bool:
    if kind is None or entry.kinds is None or kind in entry.kinds:
        return True
    return kind in AUDIO_KINDS and bool(entry.kinds & AUDIO_KINDS)


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """True when ``needle`` occurs as a contiguous run inside ``haystack``."""
    size = len(needle)
    return any(haystack[i : i + size] == needle for i in range(len(haystack) - size + 1))


def _phrase_bonus(query: tuple[str, ...], entry: AliasEntry) -> float:
    for phrase in entry.phrases:
        if phrase == query:
            return PHRASE_BONUS
        if len(phrase) >= 2 and _contains(query, phrase):
            return PHRASE_BONUS
        if len(query) >= 2 and _contains(phrase, query):
            return PHRASE_BONUS
    return 0.0


def _score(query: tuple[str, ...], entry: AliasEntry, idf: dict[str, float]) -> float:
    unknown = math.log(1 + len(load_entries()))  # a token no entry uses weighs like a rare one
    weights = {t: idf.get(t, unknown) for t in query}
    total = sum(weights.values())
    coverage = sum(w for t, w in weights.items() if t in entry.tokens) / total
    if coverage < MIN_COVERAGE:
        return 0.0
    return coverage + _phrase_bonus(query, entry)


def expand_query(query: str, kind: Kind | None = None, *, max_expansions: int = 3) -> list[str]:
    """Up to ``max_expansions`` canonical meme names matching ``query``, best first.

    Names equal to the query itself (after normalisation, or with the same content tokens) are
    never returned. With ``kind``, entries whose ``kinds`` exclude it are skipped (the two audio
    kinds count as compatible with each other).
    """
    tokens = tokenize(query)
    if not tokens or max_expansions <= 0:
        return []
    query_tokens = tuple(dict.fromkeys(tokens))  # dedupe, keep order
    normalized = normalize(query)
    idf = _idf()
    scored: list[tuple[float, int, str]] = []
    for index, entry in enumerate(load_entries()):
        if not _kind_ok(entry, kind):
            continue
        if normalize(entry.name) == normalized or tokenize(entry.name) == tokens:
            continue
        score = _score(query_tokens, entry, idf)
        if score > 0.0:
            scored.append((-score, index, entry.name))
    scored.sort()
    return [name for _, _, name in scored[:max_expansions]]


AUDIO_FILLER: frozenset[str] = frozenset(
    {
        "bgm", "ost", "soundtrack", "music", "song", "theme", "sfx", "sound", "effect",
        "effects", "audio", "track", "instrumental",
    }
)  # fmt: skip
"""Words that only say "this is audio"; ``remix`` is kept (it names a different track)."""
MEME_FILLER: frozenset[str] = frozenset({"meme", "template"})
"""Visual-meme words, stripped (with the phrase "green screen") only for audio kinds."""
_GREEN_SCREEN_RE = re.compile(r"\bgreen[\s-]*screen\b", re.IGNORECASE)
_EDGE_PUNCT = "\"'.,!?;:()[]{}"


def core_query(query: str, kind: Kind | None = None) -> str:
    """``query`` without audio filler words ("undertale bgm" -> "undertale").

    Audio filler (``AUDIO_FILLER``) is always dropped; with an audio ``kind`` the meme words
    ``meme`` / ``template`` and the phrase "green screen" go too. Matching is case-insensitive,
    other words keep their spelling, whitespace is collapsed, and the original query is returned
    when nothing would be left.
    """
    text = query
    drop = AUDIO_FILLER
    if kind in AUDIO_KINDS:
        text = _GREEN_SCREEN_RE.sub(" ", text)
        drop = AUDIO_FILLER | MEME_FILLER
    words = [w for w in text.split() if w.strip(_EDGE_PUNCT).lower() not in drop]
    core = " ".join(words)
    return core if core else " ".join(query.split())


def related_audio_kind(kind: Kind | None) -> Kind | None:
    """The other audio kind (music <-> sfx); None for non-audio kinds."""
    if kind == Kind.AUDIO_MUSIC:
        return Kind.AUDIO_SFX
    if kind == Kind.AUDIO_SFX:
        return Kind.AUDIO_MUSIC
    return None
