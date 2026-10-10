"""Prompt text and answer parsing shared by every assist backend."""

import json
from typing import Any

from pydantic import ValidationError

from clipforage.assist.base import AssistError
from clipforage.models import AssistResult, Kind

KIND_VALUES: tuple[str, ...] = tuple(k.value for k in Kind)

SYSTEM_PROMPT = f"""\
You help users of ClipForage, an open-source resource finder for video editors. ClipForage \
searches YouTube and free stock / public-domain sites (Pexels, Pixabay, Openverse, Wikimedia \
Commons, Freesound, Internet Archive, Klipy, Google Fonts) for green-screen memes, B-roll, \
overlays, sound effects, music, fonts and LUTs. Users often remember only fragments of a meme, \
sound or template, not its name. Identify what they mean and turn it into search queries.

Asset kinds: {", ".join(KIND_VALUES)}.

Return ONLY a JSON object, with no prose and no code fences, with exactly these keys:
- "queries": 2 to 6 short search-engine style strings (a few words each, no quotes or \
operators) suitable for YouTube and stock sites, best first.
- "names": canonical names of the meme, track, sound or template (empty list if unsure).
- "kind_hint": one of the asset kinds above, or null if unclear.
- "notes": one or two sentences on what it is and why these queries.
- "rights_note": if the thing is commercial copyrighted material (a game or film soundtrack, a \
label-released song, a TV clip), say so and suggest cleared alternatives (royalty-free or \
public-domain search terms); otherwise null.

Rules:
- When the style is meme or green_screen, include "green screen" variants of the best queries.
- For audio (sound effects or music), include both a music query variant and a sound-effect \
query variant.
- Do not invent names; if you are not sure what it is, leave "names" empty and keep queries \
descriptive."""

MAX_QUERIES = 6
RESULT_FIELDS: tuple[str, ...] = ("queries", "names", "kind_hint", "notes", "rights_note")

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
        "names": {"type": "array", "items": {"type": "string"}},
        "kind_hint": {"anyOf": [{"type": "string", "enum": list(KIND_VALUES)}, {"type": "null"}]},
        "notes": {"type": "string"},
        "rights_note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": list(RESULT_FIELDS),
    "additionalProperties": False,
}
"""JSON schema of the answer object (the backend-independent part of ``AssistResult``)."""


def build_prompt(text: str, kind: Kind | None, style: str | None) -> tuple[str, str]:
    """Return ``(system, user)`` prompts for a free-text description."""
    lines = [f"Description: {text.strip()}"]
    if kind is not None:
        lines.append(f"Wanted kind: {kind.value}")
    if style and style != "any":
        lines.append(f"Editing style: {style}")
    return SYSTEM_PROMPT, "\n".join(lines)


def _first_object(raw: str) -> str | None:
    """Return the first balanced ``{...}`` in ``raw`` (braces inside strings are skipped)."""
    start = raw.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start : i + 1]
        start = raw.find("{", start + 1)
    return None


def _snippet(raw: str, limit: int = 200) -> str:
    flat = " ".join(raw.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def parse_result(raw: str, backend: str, model: str | None) -> AssistResult:
    """Parse a model answer into an ``AssistResult``.

    Tolerates code fences and leading or trailing prose: the first balanced ``{...}`` is used.
    Raises ``AssistError`` when no JSON object is found or it does not validate.
    """
    candidate = _first_object(raw)
    if candidate is None:
        raise AssistError(f"{backend} returned no JSON object: {_snippet(raw)!r}")
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AssistError(f"{backend} returned invalid JSON ({exc}): {_snippet(raw)!r}") from None
    if not isinstance(data, dict):
        raise AssistError(f"{backend} returned JSON that is not an object: {_snippet(raw)!r}")
    if data.get("kind_hint") not in KIND_VALUES:
        data["kind_hint"] = None  # an unknown kind is a hint we ignore, not a failure
    payload = {k: data[k] for k in RESULT_FIELDS if k in data}
    try:
        result = AssistResult(**payload, backend=backend, model=model)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise AssistError(f"{backend} returned an unexpected answer ({problems})") from None
    queries = [q.strip() for q in result.queries if q.strip()][:MAX_QUERIES]
    if not queries:
        raise AssistError(f"{backend} returned no search queries: {_snippet(raw)!r}")
    return result.model_copy(update={"queries": queries})
