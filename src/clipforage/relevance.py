"""Small relevance guards for ``service.search_expanded`` audio searches.

Two bounded checks, no scoring model: a noise filter for the related audio section (long-form
mixes, audiobooks, podcasts and over-long items) and a stable demotion of primary audio results
that look off-topic (noise title, or no shared query token and a long or unknown length).
"""

import re
from collections.abc import Sequence

from clipforage import recall
from clipforage.models import Asset, Kind

RELATED_MAX_DURATION_S: dict[Kind, float] = {
    Kind.AUDIO_MUSIC: 600.0,
    Kind.AUDIO_SFX: 60.0,
}
"""Duration cap (s) for the related section, per related kind. Unknown durations are kept."""

RELATED_NOISE_PATTERNS: tuple[str, ...] = (
    r"audio\s*books?",
    r"mix\s*tapes?",
    r"dj\s+(?:mix|set)(?:es|s)?",
    r"full\s+album",
    r"live\s+sets?",
    r"podcasts?",
    r"radio\s+shows?",
    r"\d+\s*(?:hours?|hrs?|h)",
)
"""Title patterns (case-insensitive, whole words) that mark long-form noise in related results."""

_RELATED_NOISE_RE = re.compile(r"\b(?:" + "|".join(RELATED_NOISE_PATTERNS) + r")\b", re.IGNORECASE)


def is_related_noise(title: str) -> bool:
    """True when ``title`` matches any ``RELATED_NOISE_PATTERNS`` entry."""
    return _RELATED_NOISE_RE.search(title) is not None


def related_max_duration(kind: Kind, current: float | None) -> float | None:
    """The related search's ``max_duration``: the kind's cap unless ``current`` is tighter."""
    cap = RELATED_MAX_DURATION_S.get(kind)
    if cap is None:
        return current
    return cap if current is None else min(current, cap)


def drop_related_noise(assets: Sequence[Asset]) -> list[Asset]:
    """``assets`` without titles matching the related noise patterns (order kept)."""
    return [a for a in assets if not is_related_noise(a.title)]


def _is_off_topic(asset: Asset, tokens: set[str], cap: float | None) -> bool:
    """Noise title, or no shared content token and a length that is unknown or over ``cap``."""
    if is_related_noise(asset.title):
        return True
    if not tokens.isdisjoint(recall.tokenize(asset.title)):
        return False
    return asset.duration_s is None or (cap is not None and asset.duration_s > cap)


def demote_off_topic(query: str, assets: Sequence[Asset], kind: Kind | None) -> list[Asset]:
    """Stable-sort off-topic ``assets`` after the rest.

    An asset is off-topic when its title matches ``RELATED_NOISE_PATTERNS``, or when it shares
    no content token with ``query`` and its duration is unknown or over the kind's
    ``RELATED_MAX_DURATION_S`` cap. Short zero-overlap tracks keep their position. Applies only
    when ``query`` has at least two words and at least one content token (``recall.tokenize``:
    lowercased, punctuation stripped, stopwords dropped, plurals folded); otherwise ``assets``
    is returned unchanged (as a new list).
    """
    if len(query.split()) < 2:
        return list(assets)
    tokens = set(recall.tokenize(query))
    if not tokens:
        return list(assets)
    cap = RELATED_MAX_DURATION_S.get(kind) if kind is not None else None
    return sorted(assets, key=lambda a: _is_off_topic(a, tokens, cap))
