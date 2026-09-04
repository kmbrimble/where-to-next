"""Pure address-parsing helpers for stage 2 location resolution. No network calls,
no sheet access — see docs/SCHEMA.md's Address-resolution notes for the detection
order this implements (coordinates, then plus code, then address string).
"""
from __future__ import annotations

import re
from urllib.parse import quote

COORD_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")

# Open Location Code alphabet — deliberately excludes 0,1,I,O and vowel-like letters
# that could be misread. Without restricting to this set, any address containing a
# literal "+" (unit numbers, "Smith + Jones Ave") would misdetect as a plus code.
OLC_CHARS = "23456789CFGHJMPQRVWX"
PLUS_CODE_RE = re.compile(
    rf"^[{OLC_CHARS}]{{4,8}}\+[{OLC_CHARS}]{{2,}}(\s+.+)?$",
    re.IGNORECASE,
)

MAPS_AT_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
MAPS_3D4D_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


def classify_address(raw: str) -> tuple[str, str]:
    """Return (kind, normalised) where kind is one of:
    "empty", "coordinates", "plus_code", "address".
    """
    v = raw.strip().strip("\"'").strip()
    if not v:
        return "empty", ""

    m = COORD_RE.match(v)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return "coordinates", v

    if PLUS_CODE_RE.match(v):
        return "plus_code", v

    return "address", v


def extract_coords_from_maps_url(url: str) -> tuple[float, float] | None:
    """Pull a lat/lng pin out of a Google Maps URL. Prefers !3d<lat>!4d<lng> (the
    actual pin) over @lat,lng (the viewport centre, which can differ from the pin).
    Short links (maps.app.goo.gl) return None — resolving those needs a redirect
    follow, which is a network call and belongs to the live-geocoding path.
    """
    if "maps.app.goo.gl" in url or "goo.gl" in url:
        return None

    m = MAPS_3D4D_RE.search(url)
    if m:
        return float(m.group(1)), float(m.group(2))

    m = MAPS_AT_RE.search(url)
    if m:
        return float(m.group(1)), float(m.group(2))

    return None


def plus_code_query(raw: str) -> str:
    """URL-encode a plus code for a geocoding request: + -> %2B, space -> %20."""
    return quote(raw.strip(), safe="")
