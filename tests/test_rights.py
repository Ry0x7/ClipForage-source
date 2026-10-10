"""Rights / copyright check: one case per rule branch of ``rights.RULES`` plus the fallback."""

import pytest

from clipforage import rights
from clipforage.models import Asset, Kind, RightsLevel


def _asset(
    license: str,
    *,
    provider: str = "stock",
    kind: Kind = Kind.VIDEO,
    commercial_ok: bool | None = None,
    title: str = "t",
    extra: dict[str, object] | None = None,
) -> Asset:
    return Asset(
        provider=provider,
        id="1",
        kind=kind,
        title=title,
        extra=extra or {},
        source_url="https://example.com/1",
        license=license,
        attribution_text="by someone",
        commercial_ok=commercial_ok,
    )


@pytest.mark.parametrize(
    ("asset", "level", "label"),
    [
        # noncommercial: any NC license, before every other rule
        (_asset("CC BY-NC 4.0", commercial_ok=False), RightsLevel.NONCOMMERCIAL, "Non-commercial"),
        (_asset("CC BY-NC-SA 3.0"), RightsLevel.NONCOMMERCIAL, "Non-commercial only"),
        (_asset("Attribution NonCommercial"), RightsLevel.NONCOMMERCIAL, "Non-commercial only"),
        (
            _asset("CC BY-NC", provider="youtube", kind=Kind.AUDIO_MUSIC),
            RightsLevel.NONCOMMERCIAL,
            "Non-commercial only",
        ),
        # youtube_audio
        (
            _asset("unknown", provider="youtube", kind=Kind.AUDIO_MUSIC),
            RightsLevel.COPYRIGHTED,
            "Copyrighted – Content ID likely",
        ),
        (
            _asset("Standard YouTube License", provider="youtube", kind=Kind.AUDIO_SFX),
            RightsLevel.COPYRIGHTED,
            "Copyrighted – Content ID likely",
        ),
        # youtube (non-audio)
        (
            _asset("unknown", provider="youtube"),
            RightsLevel.COPYRIGHTED,
            "Copyrighted – Content ID likely",
        ),
        # youtube with an open license falls through to attribution
        (
            _asset("Creative Commons Attribution license (reuse allowed)", provider="youtube"),
            RightsLevel.ATTRIBUTION,
            "Credit required (Creative Commons Attribution license (reuse allowed))",
        ),
        # reserved
        (_asset("All rights reserved"), RightsLevel.COPYRIGHTED, "Copyrighted"),
        (_asset("Fair use"), RightsLevel.COPYRIGHTED, "Copyrighted"),
        # clear
        (_asset("CC0 1.0", commercial_ok=True), RightsLevel.CLEAR, "Free to use (CC0 1.0)"),
        (_asset("CC0-1.0"), RightsLevel.CLEAR, "Free to use (CC0-1.0)"),
        (_asset("Public Domain Mark"), RightsLevel.CLEAR, "Free to use (Public Domain Mark)"),
        (_asset("Public domain"), RightsLevel.CLEAR, "Free to use (Public domain)"),
        (_asset("PDM"), RightsLevel.CLEAR, "Free to use (PDM)"),
        (_asset("Pexels License"), RightsLevel.CLEAR, "Free to use (Pexels License)"),
        (
            _asset("Pixabay Content License"),
            RightsLevel.CLEAR,
            "Free to use (Pixabay Content License)",
        ),
        # attribution
        (_asset("CC BY 4.0"), RightsLevel.ATTRIBUTION, "Credit required (CC BY 4.0)"),
        (_asset("CC BY-SA 4.0"), RightsLevel.ATTRIBUTION, "Credit required (CC BY-SA 4.0)"),
        (_asset("cc-by-3.0"), RightsLevel.ATTRIBUTION, "Credit required (cc-by-3.0)"),
        (_asset("OFL-1.1"), RightsLevel.ATTRIBUTION, "Credit required (OFL-1.1)"),
        (_asset("Apache-2.0"), RightsLevel.ATTRIBUTION, "Credit required (Apache-2.0)"),
        # attribution_nd: still attribution, but no edits
        (_asset("CC BY-ND 4.0"), RightsLevel.ATTRIBUTION, "Credit required, no edits (CC BY-ND)"),
        (
            _asset("Attribution-NoDerivs 3.0"),
            RightsLevel.ATTRIBUTION,
            "Credit required, no edits (CC BY-ND)",
        ),
        (
            _asset("Creative Commons Attribution-NoDerivatives 4.0"),
            RightsLevel.ATTRIBUTION,
            "Credit required, no edits (CC BY-ND)",
        ),
        (_asset("CC BY-NC-ND 4.0"), RightsLevel.NONCOMMERCIAL, "Non-commercial only"),
        # commercial_false
        (_asset("Sampling+", commercial_ok=False), RightsLevel.NONCOMMERCIAL, "Non-commercial"),
        # soundtrack_upload: audio, unknown license, soundtrack-like title / description
        (
            _asset(
                "unknown",
                provider="internet_archive",
                kind=Kind.AUDIO_MUSIC,
                title="Undertale Soundtrack: Megalovania",
            ),
            RightsLevel.COPYRIGHTED,
            "Likely copyrighted (soundtrack upload)",
        ),
        (
            _asset(
                "Unknown",
                provider="wikimedia",
                kind=Kind.AUDIO_SFX,
                title="Persona 5 OST - Wake Up",
            ),
            RightsLevel.COPYRIGHTED,
            "Likely copyrighted (soundtrack upload)",
        ),
        (
            _asset("unknown", kind=Kind.AUDIO_MUSIC, title="Naruto Opening 1"),
            RightsLevel.COPYRIGHTED,
            "Likely copyrighted (soundtrack upload)",
        ),
        (
            _asset("unknown", kind=Kind.AUDIO_MUSIC, title="Zelda BGM loop"),
            RightsLevel.COPYRIGHTED,
            "Likely copyrighted (soundtrack upload)",
        ),
        (
            _asset(
                "unknown",
                kind=Kind.AUDIO_MUSIC,
                title="track 01",
                extra={"description": "From the Original Soundtrack of Celeste"},
            ),
            RightsLevel.COPYRIGHTED,
            "Likely copyrighted (soundtrack upload)",
        ),
        # ... but not video, not with a known license, and not "ghost" / "boost"
        (
            _asset("unknown", kind=Kind.VIDEO, title="Undertale Soundtrack"),
            RightsLevel.UNKNOWN,
            "Check license at source",
        ),
        (
            _asset("CC BY 4.0", kind=Kind.AUDIO_MUSIC, title="My game OST"),
            RightsLevel.ATTRIBUTION,
            "Credit required (CC BY 4.0)",
        ),
        (
            _asset("unknown", kind=Kind.AUDIO_SFX, title="ghost boost whoosh"),
            RightsLevel.UNKNOWN,
            "Check license at source",
        ),
        # fallback
        (_asset("unknown"), RightsLevel.UNKNOWN, "Check license at source"),
        (_asset("GFDL", commercial_ok=True), RightsLevel.UNKNOWN, "Check license at source"),
    ],
)
def test_classify(asset: Asset, level: RightsLevel, label: str) -> None:
    result = rights.classify(asset)
    assert result.level == level
    assert result.label.startswith(label)
    assert result.detail.endswith(".")


def test_every_rule_is_covered_by_a_name() -> None:
    assert [r.name for r in rights.RULES] == [
        "noncommercial",
        "youtube_audio",
        "youtube",
        "reserved",
        "mixkit_unverified",
        "mixkit_restricted",
        "mixkit_free",
        "lottie_simple",
        "clear",
        "attribution_nd",
        "attribution",
        "commercial_false",
        "soundtrack_upload",
    ]


def test_nd_detail_forbids_edits() -> None:
    detail = rights.classify(_asset("CC BY-ND 4.0")).detail
    assert "cutting, remixing" in detail and "not allowed" in detail


def test_soundtrack_detail_mentions_content_id_and_cleared_sources() -> None:
    asset = _asset("unknown", kind=Kind.AUDIO_MUSIC, title="Undertale Soundtrack")
    detail = rights.classify(asset).detail
    assert "Content ID" in detail and "rights holder" in detail and "cleared" in detail


@pytest.mark.parametrize(
    ("level", "cleared"),
    [
        (RightsLevel.CLEAR, True),
        (RightsLevel.ATTRIBUTION, True),
        (RightsLevel.NONCOMMERCIAL, False),
        (RightsLevel.COPYRIGHTED, False),
        (RightsLevel.UNKNOWN, False),
    ],
)
def test_is_cleared(level: RightsLevel, cleared: bool) -> None:
    assert rights.is_cleared(level) is cleared


def test_youtube_audio_detail_mentions_content_id_and_cleared_sources() -> None:
    detail = rights.classify(_asset("unknown", provider="youtube", kind=Kind.AUDIO_MUSIC)).detail
    assert "Content ID" in detail
    assert "freesound" in detail and "pixabay" in detail


@pytest.mark.parametrize(
    ("license", "commercial_ok", "level", "label", "detail_part"),
    [
        (
            "Mixkit License",
            True,
            RightsLevel.CLEAR,
            "Free to use (Mixkit License)",
            "no attribution required",
        ),
        (
            "Mixkit License",
            None,
            RightsLevel.UNKNOWN,
            "Check license at source",
            "license is not known",
        ),
        (
            "Mixkit Restricted License",
            None,
            RightsLevel.UNKNOWN,
            "Mixkit Restricted – check terms",
            "source page",
        ),
        (
            "Mixkit License (Free or Restricted, check source)",
            None,
            RightsLevel.UNKNOWN,
            "Check Mixkit tier at source",
            "source page",
        ),
        (
            "Mixkit License (Free or Restricted, check source)",
            True,
            RightsLevel.UNKNOWN,
            "Check Mixkit tier at source",
            "source page",
        ),
        (
            "Lottie Simple License (unverified)",
            None,
            RightsLevel.UNKNOWN,
            "Lottie Simple License (unverified)",
            "does not expose a per-item license",
        ),
    ],
)
def test_scraped_provider_licenses(
    license: str,
    commercial_ok: bool | None,
    level: RightsLevel,
    label: str,
    detail_part: str,
) -> None:
    result = rights.classify(_asset(license, commercial_ok=commercial_ok))
    assert result.level == level
    assert result.label == label
    assert detail_part in result.detail
    assert result.detail.endswith(".")


def test_mixkit_free_detail_forbids_raw_redistribution() -> None:
    detail = rights.classify(_asset("Mixkit License", commercial_ok=True)).detail
    assert "redistribut" in detail
