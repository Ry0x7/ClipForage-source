"""Link-out directory: free sources without a public search API (never scraped).

These are shown to the user as places to browse by hand; clipforage never searches or
downloads from them. Mixkit and LottieFiles used to be listed here; they are now searched by
the (throttled, allowlisted) scraping providers ``mixkit`` and ``lottiefiles``.
"""

from pydantic import BaseModel, ConfigDict, Field

from clipforage.models import Kind


class LinkOut(BaseModel):
    """A site the user can open in a browser; not searched by clipforage."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")
    kinds: list[Kind] = Field(min_length=1)
    license_note: str = Field(min_length=1)
    requires_account: bool


LINKOUTS: list[LinkOut] = [
    LinkOut(
        name="Videvo",
        url="https://www.freepik.com/videos",
        kinds=[Kind.VIDEO],
        license_note="now Freepik Videos (videvo.net redirects); mixed: free clips need "
        "attribution, check each",
        requires_account=True,
    ),
    LinkOut(
        name="Zapsplat",
        url="https://www.zapsplat.com/",
        kinds=[Kind.AUDIO_SFX],
        license_note="free with attribution or paid Gold",
        requires_account=True,
    ),
    LinkOut(
        name="Uppbeat",
        url="https://uppbeat.io/",
        kinds=[Kind.AUDIO_MUSIC],
        license_note="free tier needs credit link",
        requires_account=True,
    ),
    LinkOut(
        name="YouTube Audio Library",
        url="https://www.youtube.com/audiolibrary",
        kinds=[Kind.AUDIO_MUSIC, Kind.AUDIO_SFX],
        license_note="YouTube Audio Library license, some need attribution",
        requires_account=True,
    ),
    LinkOut(
        name="Pixabay Music",
        url="https://pixabay.com/music/",
        kinds=[Kind.AUDIO_MUSIC],
        license_note="Pixabay Content License",
        requires_account=False,
    ),
    LinkOut(
        name="RocketStock free LUTs",
        url="https://www.shutterstock.com/blog/free-luts-for-log-footage",
        kinds=[Kind.LUT],
        license_note="now a Shutterstock blog post (rocketstock.com redirects); "
        "check each pack's terms",
        requires_account=False,
    ),
    LinkOut(
        name="IWLTBAP free LUTs",
        url="https://iwltbap.com/",
        kinds=[Kind.LUT],
        license_note="free pack, check site terms",
        requires_account=False,
    ),
    LinkOut(
        name="PlayPhrase",
        url="https://www.playphrase.me/",
        kinds=[Kind.VIDEO],
        license_note="Movie quote lookup; clips are copyrighted, personal/educational use only; "
        "no API",
        requires_account=False,
    ),
    LinkOut(
        name="TikTok",
        url="https://www.tiktok.com/",
        kinds=[Kind.VIDEO],
        license_note="browse only: clipforage does not search or download TikTok; nearly all "
        "content is copyrighted",
        requires_account=False,
    ),
    LinkOut(
        name="Instagram Reels",
        url="https://www.instagram.com/",
        kinds=[Kind.VIDEO],
        license_note="browse only: clipforage does not search or download Instagram; nearly all "
        "content is copyrighted",
        requires_account=True,
    ),
]


def linkouts_for(kind: Kind | None) -> list[LinkOut]:
    """Return link-outs that offer `kind` (all of them when `kind` is None), in listed order."""
    if kind is None:
        return list(LINKOUTS)
    return [link for link in LINKOUTS if kind in link.kinds]
