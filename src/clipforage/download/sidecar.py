"""Write the ``<file>.json`` license sidecar next to a downloaded media file."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforage import __version__
from clipforage.models import Asset


def sidecar_path(media_path: Path) -> Path:
    """Return the sidecar path for a media file, e.g. ``clip.mp4`` -> ``clip.mp4.json``."""
    return media_path.with_name(media_path.name + ".json")


def write_sidecar(
    media_path: Path,
    asset: Asset,
    fetched_at: datetime,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write license/attribution metadata for ``media_path`` and return the sidecar path."""
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    data: dict[str, Any] = {
        "license": asset.license,
        "attribution_text": asset.attribution_text,
        "source_url": asset.source_url,
        "provider": asset.provider,
        "fetched_at": fetched_at.astimezone(UTC).isoformat(),
        "asset_id": asset.id,
        "title": asset.title,
        "kind": str(asset.kind),
        "commercial_ok": asset.commercial_ok,
        "clipforage_version": __version__,
    }
    if extra:
        data.update(extra)
    path = sidecar_path(media_path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
