import pytest
from pydantic import ValidationError

from clipforage.download.service import parse_clip
from clipforage.models import Asset, Cue, Kind, PhraseHit

ASSET = Asset(
    provider="youtube",
    id="abc123",
    kind=Kind.VIDEO,
    title="t",
    source_url="https://www.youtube.com/watch?v=abc123",
    license="unknown",
    attribution_text="a",
    commercial_ok=None,
)


def hit(start: float, end: float) -> PhraseHit:
    return PhraseHit(asset=ASSET, start_s=start, end_s=end, text="x")


def test_ref_and_defaults() -> None:
    h = hit(1.0, 2.0)
    assert h.ref == "youtube:abc123"
    assert h.lang is None
    assert h.auto_generated is False


@pytest.mark.parametrize(
    ("start", "end", "pad", "spec"),
    [
        (42.0, 45.0, 1.0, "41.0-46.0"),
        (0.4, 2.0, 1.0, "0.0-3.0"),  # start < pad clamps at 0
        (0.0, 0.0, 1.0, "0.0-1.0"),
        (1.0, 1.0, 1.0, "0.0-2.0"),
        (12.84, 14.639, 1.0, "11.8-15.6"),
        (10.0, 12.0, 0.0, "10.0-12.0"),
        (5.0, 5.0, 0.0, "5.0-5.1"),  # end is always after start
        (3723.4, 3726.0, 2.5, "3720.9-3728.5"),
    ],
)
def test_clip_spec(start: float, end: float, pad: float, spec: str) -> None:
    assert hit(start, end).clip_spec(pad) == spec
    clip = parse_clip(spec)
    assert clip[0] >= 0.0
    assert clip[1] > clip[0]


def test_clip_spec_default_pad() -> None:
    assert hit(42.0, 45.0).clip_spec() == "41.0-46.0"


def test_models_are_frozen() -> None:
    cue = Cue(start_s=1.0, end_s=2.0, text="hi")
    with pytest.raises(ValidationError):
        cue.text = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        hit(1.0, 2.0).start_s = 0.0  # type: ignore[misc]
