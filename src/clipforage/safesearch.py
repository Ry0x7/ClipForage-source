"""Safe search: a shared, table-driven post-filter for explicit results (on by default).

The blocklist lives in ``providers/data/safesearch_terms.json``. Text is lowercased and every
run of non-alphanumerics (including ``_ - . /``) becomes one space; a term (single word or
phrase) matches only as whole words, so "Essex", "assess" or "Scunthorpe" never match.

Two tiers: strong ``terms`` match the title, ``extra`` keys ``description``/``tags``/
``subject``/``collection``/``categories`` (str or list of str) and the path of ``source_url``.
``mild_terms`` (ambiguous words such as "naked" or "sex") match the same fields except the
description and the URL path (an Internet Archive identifier is a weak signal). Before matching,
every ``allow_phrases`` entry ("naked ladies", "sex pistols") is cut out of the text and replaced
by a separator, so its words match nothing and the words around it do not join into a phrase.
Internet Archive collection identifiers in ``ia_collections`` also match ``extra["collection"]``
exactly (case-insensitive).

The setting is ``[search] safe`` in config.toml or ``CLIPFORAGE_SAFE_SEARCH`` (default on).
"""

import json
import re
from collections.abc import Iterable
from functools import cache
from importlib import resources
from typing import Any
from urllib.parse import unquote, urlparse

from clipforage.config import get_bool_setting
from clipforage.models import Asset

TERMS_FILE = "safesearch_terms.json"
EXTRA_KEYS = ("description", "tags", "subject", "collection", "categories")
MILD_KEYS = ("tags", "subject", "collection", "categories")  # plus the title
_NON_WORD = re.compile(r"[\W_]+")
_CUT = "|"  # replaces an allow phrase; never a word after normalize()


def configured_safe() -> bool:
    """``[search] safe`` / ``CLIPFORAGE_SAFE_SEARCH``; True when unset."""
    return get_bool_setting("search", "safe", env="SAFE_SEARCH", default=True)


def resolve_safe(safe: bool | None) -> bool:
    """An explicit ``safe`` wins; None means the configured default."""
    return configured_safe() if safe is None else safe


def normalize(text: str) -> str:
    """Lowercase ``text`` with non-alphanumeric runs collapsed to single spaces."""
    return _NON_WORD.sub(" ", text.lower()).strip()


@cache
def _table() -> dict[str, Any]:
    raw = resources.files("clipforage.providers.data").joinpath(TERMS_FILE).read_text("utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


@cache
def _terms(key: str = "terms") -> tuple[frozenset[str], tuple[str, ...]]:
    """(single-word terms, multi-word phrases) of table list ``key``, both normalized."""
    words: set[str] = set()
    phrases: list[str] = []
    for term in _table().get(key, []):
        norm = normalize(str(term))
        if not norm:
            continue
        if " " in norm:
            phrases.append(norm)
        else:
            words.add(norm)
    return frozenset(words), tuple(dict.fromkeys(phrases))


@cache
def _allow_pattern() -> re.Pattern[str] | None:
    """Whole-word regex for the normalized ``allow_phrases`` (longest first), or None."""
    phrases = {normalize(str(p)) for p in _table().get("allow_phrases", [])} - {""}
    if not phrases:
        return None
    alternatives = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<!\S)(?:{alternatives})(?!\S)")


def _hits(norm: str, key: str) -> bool:
    words, phrases = _terms(key)
    if not words.isdisjoint(norm.split()):
        return True
    padded = f" {norm} "
    return any(f" {phrase} " in padded for phrase in phrases)


def ia_collections() -> list[str]:
    """Internet Archive adult collection identifiers (as listed in the table)."""
    return [str(c) for c in _table().get("ia_collections", [])]


def ia_subjects() -> list[str]:
    """Explicit subject terms excluded from Internet Archive queries."""
    return [str(s) for s in _table().get("ia_subjects", [])]


def text_is_explicit(text: str, *, mild: bool = True) -> bool:
    """True if ``text`` contains a strong (or, with ``mild``, a mild) term as whole words."""
    norm = normalize(text)
    pattern = _allow_pattern()
    if norm and pattern is not None:
        norm = pattern.sub(_CUT, norm)
    if not norm:
        return False
    return _hits(norm, "terms") or (mild and _hits(norm, "mild_terms"))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [v for v in value if isinstance(v, str)]
    return []


def is_explicit(asset: Asset) -> bool:
    """True if the title, tag-like ``extra`` fields or URL path hit the blocklist (see tiers)."""
    if text_is_explicit(asset.title):
        return True
    collections = {c.lower() for c in _strings(asset.extra.get("collection"))}
    if collections and not collections.isdisjoint(c.lower() for c in ia_collections()):
        return True
    for key in EXTRA_KEYS:
        mild = key in MILD_KEYS
        if any(text_is_explicit(s, mild=mild) for s in _strings(asset.extra.get(key))):
            return True
    try:
        path = unquote(urlparse(asset.source_url).path)
    except ValueError:
        path = ""
    return text_is_explicit(path, mild=False)


def filter_explicit(assets: Iterable[Asset]) -> tuple[list[Asset], int]:
    """Split off explicit assets: return ``(kept, dropped_count)``."""
    kept: list[Asset] = []
    dropped = 0
    for asset in assets:
        if is_explicit(asset):
            dropped += 1
        else:
            kept.append(asset)
    return kept, dropped
