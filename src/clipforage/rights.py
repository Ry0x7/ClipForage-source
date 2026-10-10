"""Quick rights / copyright check: map an asset's license info to a UI-ready ``Rights`` signal.

Rules are a table checked top to bottom; the first match wins. This is a heuristic based on the
license string the provider reported, not legal advice: the label always tells the user what to
check or do.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from clipforage.models import Asset, Kind, Rights, RightsLevel

__all__ = ["CLEARED_LEVELS", "RULES", "Rights", "RightsLevel", "classify", "is_cleared"]

AUDIO_KINDS: frozenset[Kind] = frozenset({Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})
_NC_RE = re.compile(r"(?:\bnc\b|non-?commercial)")
_RESERVED_MARKERS: tuple[str, ...] = (
    "all rights reserved",
    "standard youtube",
    "fair use",
)
_CLEAR_MARKERS: tuple[str, ...] = (
    "cc0",
    "public domain",
    "publicdomain",
    "pexels",
    "pixabay",
)
_CLEAR_RE = re.compile(r"(?:\bpdm\b|^pd\b|^pd-)")
_ND_RE = re.compile(r"(?:\bnd\b|no-?derivs?|no-?derivatives)")
_SOUNDTRACK_RE = re.compile(
    r"\b(?:ost|soundtrack|full album|theme song|opening|ending|bgm|original sound ?track)\b",
    re.IGNORECASE,
)
_UNKNOWN_LICENSES: frozenset[str] = frozenset({"", "unknown"})
CLEARED_LEVELS: frozenset[RightsLevel] = frozenset({RightsLevel.CLEAR, RightsLevel.ATTRIBUTION})
_ATTRIBUTION_RE = re.compile(
    r"(?:\bcc[ -]?by\b|attribution|\bofl\b|\bapache\b|\bmit\b|\bufl\b|\bgpl\b)"
)


def _lic(asset: Asset) -> str:
    return asset.license.strip().lower()


def _is_nc(asset: Asset) -> bool:
    return bool(_NC_RE.search(_lic(asset)))


def _is_clear(asset: Asset) -> bool:
    lic = _lic(asset)
    return any(m in lic for m in _CLEAR_MARKERS) or bool(_CLEAR_RE.search(lic))


def _is_attribution(asset: Asset) -> bool:
    return bool(_ATTRIBUTION_RE.search(_lic(asset)))


def _is_nd(asset: Asset) -> bool:
    """An attribution license that forbids derivatives (CC BY-ND, "NoDerivs")."""
    return _is_attribution(asset) and bool(_ND_RE.search(_lic(asset)))


def _soundtrack_upload(asset: Asset) -> bool:
    """Audio with no known license whose title / description reads like a soundtrack rip."""
    if asset.kind not in AUDIO_KINDS or _lic(asset) not in _UNKNOWN_LICENSES:
        return False
    description = asset.extra.get("description")
    text = f"{asset.title} {description if isinstance(description, str) else ''}"
    return bool(_SOUNDTRACK_RE.search(text))


def _youtube_unlicensed(asset: Asset) -> bool:
    """A youtube result without an open (CC / public domain) license."""
    return asset.provider == "youtube" and not (_is_clear(asset) or _is_attribution(asset))


def _is_mixkit_unverified(asset: Asset) -> bool:
    """Mixkit result whose tier (Free or Restricted) the list page did not state."""
    return _lic(asset) == "mixkit license (free or restricted, check source)"


def _is_mixkit_restricted(asset: Asset) -> bool:
    return _lic(asset).startswith("mixkit restricted license")


def _is_mixkit_free(asset: Asset) -> bool:
    return _lic(asset).startswith("mixkit license") and asset.commercial_ok is True


def _is_lottie_simple(asset: Asset) -> bool:
    return _lic(asset) == "lottie simple license (unverified)"


def _is_reserved(asset: Asset) -> bool:
    lic = _lic(asset)
    return any(m in lic for m in _RESERVED_MARKERS)


@dataclass(frozen=True)
class Rule:
    """One row of the rights table: when ``matches`` holds, the asset gets this signal."""

    name: str
    level: RightsLevel
    matches: Callable[[Asset], bool]
    label: str
    """May contain ``{license}``."""
    detail: str
    """May contain ``{license}``."""


RULES: tuple[Rule, ...] = (
    Rule(
        "noncommercial",
        RightsLevel.NONCOMMERCIAL,
        _is_nc,
        "Non-commercial only",
        "Licensed {license}: credit the author and keep it out of monetized, sponsored or client "
        "work.",
    ),
    Rule(
        "youtube_audio",
        RightsLevel.COPYRIGHTED,
        lambda a: _youtube_unlicensed(a) and a.kind in AUDIO_KINDS,
        "Copyrighted – Content ID likely",
        "This track is likely claimed by Content ID; search cleared sources (freesound, pixabay) "
        "instead, or use it only where the platform allows it.",
    ),
    Rule(
        "youtube",
        RightsLevel.COPYRIGHTED,
        _youtube_unlicensed,
        "Copyrighted – Content ID likely",
        "Third-party YouTube upload with no open license; expect Content ID claims and use it "
        "only where fair use or the platform's rules allow.",
    ),
    Rule(
        "reserved",
        RightsLevel.COPYRIGHTED,
        _is_reserved,
        "Copyrighted",
        "Licensed {license}: get the rights holder's permission before using it.",
    ),
    Rule(
        "mixkit_unverified",
        RightsLevel.UNKNOWN,
        _is_mixkit_unverified,
        "Check Mixkit tier at source",
        "Mixkit items are either Free or Restricted and this one's tier is not known; open the "
        "source page and check which license applies before use.",
    ),
    Rule(
        "mixkit_restricted",
        RightsLevel.UNKNOWN,
        _is_mixkit_restricted,
        "Mixkit Restricted – check terms",
        "Under the Mixkit Restricted License, which limits some uses; read the terms on the "
        "source page before using it.",
    ),
    Rule(
        "mixkit_free",
        RightsLevel.CLEAR,
        _is_mixkit_free,
        "Free to use (Mixkit License)",
        "Free for personal and commercial edits with no attribution required; Mixkit terms "
        "forbid redistributing the raw file.",
    ),
    Rule(
        "lottie_simple",
        RightsLevel.UNKNOWN,
        _is_lottie_simple,
        "Lottie Simple License (unverified)",
        "The LottieFiles API does not expose a per-item license; confirm the license on the "
        "item page before use.",
    ),
    Rule(
        "clear",
        RightsLevel.CLEAR,
        _is_clear,
        "Free to use ({license})",
        "Free for personal and commercial edits without credit; do not resell the file unaltered.",
    ),
    Rule(
        "attribution_nd",
        RightsLevel.ATTRIBUTION,
        _is_nd,
        "Credit required, no edits (CC BY-ND)",
        "Licensed {license}: credit the author and use it unchanged; cutting, remixing or "
        "otherwise editing it is not allowed.",
    ),
    Rule(
        "attribution",
        RightsLevel.ATTRIBUTION,
        _is_attribution,
        "Credit required ({license})",
        "Credit the author with the attribution text and check share-alike or no-derivatives "
        "terms at the source.",
    ),
    Rule(
        "commercial_false",
        RightsLevel.NONCOMMERCIAL,
        lambda a: a.commercial_ok is False,
        "Non-commercial only",
        "The source does not allow commercial use of {license} content; keep it out of "
        "monetized work.",
    ),
    Rule(
        "soundtrack_upload",
        RightsLevel.COPYRIGHTED,
        _soundtrack_upload,
        "Likely copyrighted (soundtrack upload)",
        "Looks like a soundtrack upload with no license: the music belongs to its rights holder "
        "and is likely claimed by Content ID; prefer cleared sources (freesound, CC0 / CC BY).",
    ),
)

_FALLBACK = Rights(
    level=RightsLevel.UNKNOWN,
    label="Check license at source",
    detail="The license is not known; open the source page and confirm the terms before use.",
)


def is_cleared(level: RightsLevel) -> bool:
    """True for levels usable in edits as-is (``clear``, ``attribution``)."""
    return level in CLEARED_LEVELS


def classify(asset: Asset) -> Rights:
    """The rights signal of the first matching rule, else ``unknown``."""
    for rule in RULES:
        if rule.matches(asset):
            return Rights(
                level=rule.level,
                label=rule.label.format(license=asset.license),
                detail=rule.detail.format(license=asset.license),
            )
    return _FALLBACK
