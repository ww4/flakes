"""Two kinds of geofence, because the two layers need different things.

LAYER 1 is a POINT test against real polygons: is his house inside the warning?
That is `point_in_geometry`, ray casting, stdlib only. NWS and SPC both publish
GeoJSON, so the tight fence is exact rather than approximated by a county.

LAYER 2 is a TERM test, because Ryan Hall talks; he does not publish polygons.
`fence_score` scores a list of region names he mentioned.

⚠️ The honest limitation, recorded here because this is where someone will come
looking for it: a large share of his geographic content is "if you live in this
shaded area" with NO place name spoken at all. The term fence cannot see those,
and no amount of tuning the word list will fix it. That is why Layer 3 also
consults SPC — the polygon covers what the words miss.
"""
from __future__ import annotations

# Strong = unambiguously his part of the world. Weak = regions that CONTAIN
# northern Kentucky but sprawl well past it, so one on its own is not enough.
STRONG_TERMS = (
    "northern kentucky", "kentucky", "ohio valley", "louisville", "lexington",
    "cincinnati", "covington", "frankfort", "bluegrass", "tri-state",
    "i-71", "i-75", "owen county", "owenton",
)
# ⚠️ These must not OVERLAP each other or a strong term, because two weak hits
# are enough to clear the fence: with "tennessee" and "tennessee valley" both
# listed, one spoken phrase would score two independent signals and a forecast
# for Nashville would read as a forecast for his house. Bare "ohio" is left out
# for the same reason — it is a substring of the strong term "ohio valley".
WEAK_TERMS = (
    "mid-south", "tennessee valley", "midwest", "mid-mississippi valley",
    "great lakes", "central appalachians", "mid-atlantic", "indiana",
)

STRONG_WEIGHT = 2
WEAK_WEIGHT = 1
# One strong term, or two weak ones. A single "Midwest" is not about him.
FENCE_THRESHOLD = 2


def fence_score(regions) -> int:
    """Score how much a list of spoken region names is about northern Kentucky.

    Scores terms, not mentions: saying "Kentucky" four times is one signal, not
    four. Substring matching is deliberate — "Northern Kentucky" must satisfy
    "kentucky", and "the I-75 corridor" must satisfy "i-75".
    """
    haystack = " ; ".join(str(r).lower() for r in (regions or []))
    strong = any(term in haystack for term in STRONG_TERMS)
    # Distinct weak terms, capped: naming five vague regions is not five
    # signals, it is one video that covered the whole country.
    weak = min(sum(1 for term in WEAK_TERMS if term in haystack), 2)
    return (STRONG_WEIGHT if strong else 0) + weak * WEAK_WEIGHT


def in_fence(regions) -> bool:
    return fence_score(regions) >= FENCE_THRESHOLD


def _ring_contains(ring, lon: float, lat: float) -> bool:
    """Ray casting across one linear ring. GeoJSON order is (lon, lat)."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        # Does the edge straddle the horizontal ray at `lat`?
        if (y1 > lat) != (y2 > lat):
            # x of the edge at y = lat
            xint = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if xint > lon:
                inside = not inside
    return inside


def _polygon_contains(rings, lon: float, lat: float) -> bool:
    """A GeoJSON Polygon: ring 0 is the exterior, the rest are holes.

    Even-odd across every ring is correct here and simpler than testing the
    exterior and subtracting holes: a point inside a hole is inside exactly two
    rings, which is even, which is outside. Holes matter — SPC risk areas are
    routinely drawn with them.
    """
    return sum(1 for r in rings if _ring_contains(r, lon, lat)) % 2 == 1


def point_in_geometry(geometry, lon: float, lat: float) -> bool:
    """True if (lon, lat) falls inside a GeoJSON Polygon or MultiPolygon."""
    if not geometry:
        return False
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return _polygon_contains(coords, lon, lat)
    if gtype == "MultiPolygon":
        return any(_polygon_contains(poly, lon, lat) for poly in coords)
    if gtype == "GeometryCollection":
        return any(point_in_geometry(g, lon, lat)
                   for g in geometry.get("geometries") or [])
    # A point with no polygon (some NWS alerts carry none — zone-only products)
    # is NOT a match. Callers that want zone semantics must ask for them; an
    # unknown geometry silently matching would turn a statewide advisory into a
    # notification about his house.
    return False
