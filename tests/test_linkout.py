import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from clipforage.cli import app
from clipforage.linkout import LINKOUTS, LinkOut, linkouts_for
from clipforage.models import Kind

runner = CliRunner()


def test_linkouts_are_valid_models() -> None:
    assert len(LINKOUTS) == 10
    for link in LINKOUTS:
        assert LinkOut.model_validate(link.model_dump()) == link
        assert link.url.startswith("https://")
        assert link.kinds
        assert link.license_note


def test_linkout_rejects_bad_data() -> None:
    good = {
        "name": "Site",
        "url": "https://example.com/",
        "kinds": ["lut"],
        "license_note": "free",
        "requires_account": False,
    }
    assert LinkOut(**good).kinds == [Kind.LUT]
    for bad in (
        {"kinds": ["banana"]},
        {"kinds": []},
        {"url": "http://example.com/"},
        {"name": ""},
        {"license_note": ""},
    ):
        with pytest.raises(ValidationError):
            LinkOut(**{**good, **bad})
    with pytest.raises(ValidationError):
        LinkOut(**{k: v for k, v in good.items() if k != "requires_account"})


def test_linkouts_for_filters_by_kind() -> None:
    names = [link.name for link in linkouts_for(Kind.LUT)]
    assert names == ["RocketStock free LUTs", "IWLTBAP free LUTs"]
    assert linkouts_for(None) == LINKOUTS
    assert linkouts_for(Kind.STICKER) == []
    assert [link.name for link in linkouts_for(Kind.AUDIO_SFX)] == [
        "Zapsplat",
        "YouTube Audio Library",
    ]


def test_scraped_sources_are_not_linkouts() -> None:
    names = {link.name for link in LINKOUTS}
    assert "Mixkit" not in names  # searched by the mixkit provider now
    assert "LottieFiles" not in names  # searched by the lottiefiles provider now


def test_links_command_lut() -> None:
    result = runner.invoke(app, ["links", "--kind", "lut"])
    assert result.exit_code == 0
    assert "RocketStock free LUTs" in result.output
    assert "IWLTBAP free LUTs" in result.output
    assert "rocketstock.com redirects" in result.output
    assert "https://iwltbap.com/" in result.output
    assert "Mixkit" not in result.output


def test_links_command_lists_all() -> None:
    result = runner.invoke(app, ["links"])
    assert result.exit_code == 0
    for link in LINKOUTS:
        assert link.name in result.output
        assert link.url in result.output
        assert link.license_note in result.output
    assert "audio_music,audio_sfx" in result.output


def test_links_command_kind_without_linkouts() -> None:
    result = runner.invoke(app, ["links", "--kind", "sticker"])
    assert result.exit_code == 0
    assert "no link-outs" in result.output


def test_links_command_marks_account_required() -> None:
    result = runner.invoke(app, ["links", "--kind", "video"])
    assert result.exit_code == 0
    assert "Instagram Reels (account required)" in result.output
    assert "TikTok (account required)" not in result.output
    assert "TikTok" in result.output
    rows = [line for line in result.output.splitlines() if not line.startswith(" ")]
    assert len({line.index(" video ") for line in rows}) == 1


def test_links_command_invalid_kind() -> None:
    result = runner.invoke(app, ["links", "--kind", "banana"])
    assert result.exit_code == 1
    assert "invalid kind" in result.output


def test_playphrase_linkout() -> None:
    (link,) = [link for link in LINKOUTS if link.name == "PlayPhrase"]
    assert link.url == "https://www.playphrase.me/"
    assert link.kinds == [Kind.VIDEO]
    assert link.requires_account is False
    assert "copyrighted" in link.license_note
    assert "no API" in link.license_note
    assert link in linkouts_for(Kind.VIDEO)


@pytest.mark.parametrize(
    ("name", "url", "requires_account"),
    [
        ("TikTok", "https://www.tiktok.com/", False),
        ("Instagram Reels", "https://www.instagram.com/", True),
    ],
)
def test_social_video_linkouts(name: str, url: str, requires_account: bool) -> None:
    (link,) = [link for link in LINKOUTS if link.name == name]
    assert link.url == url
    assert link.kinds == [Kind.VIDEO]
    assert link.requires_account is requires_account
    assert "does not search or download" in link.license_note
    assert "nearly all content is copyrighted" in link.license_note
    assert link in linkouts_for(Kind.VIDEO)
