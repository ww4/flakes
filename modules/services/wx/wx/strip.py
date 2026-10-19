"""The weather strip — one rendered card for the dashboard's Weather tab.

Homepage cannot build this itself, which is the whole reason this module
exists. Its `openmeteo` info widget shows current conditions only, `customapi`
renders text rows with no icons, and nothing in Homepage can reach this
database for the Ryan Hall read. So wx renders a small HTML page every 15
minutes and the dashboard iframes it, the same pattern already used there for
the Grafana temperature and river embeds.

Three things end up on the strip:

  1. Current conditions, from Open-Meteo. Chosen because it needs no API key
     at all — one less secret to mint, rotate and forget.
  2. The latest Ryan Hall read, out of the `extraction` table that layer 2
     already fills. ⚠️ Only shown while it is still RELEVANT (see
     `latest_read`) — a forecast from three weeks ago rendered in the present
     tense is worse than no forecast at all.
  3. A ten-day row with icons, derived from the same Open-Meteo call.

⚠️ PII. `load_location` returns Chris's home coordinates and this module is
handed them. They must NOT reach the rendered page: ww4/flakes is public on
GitHub, and the strip is served to anything on the tailnet. Coordinates are
used to build the request URL and are never written into the output — there is
a test that asserts exactly that.

⚠️ NO KEEP-LAST-GOOD. When Open-Meteo cannot be reached the page says so, in
place, with the time of the attempt. Re-serving the previous render would turn
a total outage into a page that looks fine and is quietly wrong, which is the
failure mode that is hardest to notice and hardest to explain afterwards.
"""
from __future__ import annotations

import html
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import geo

API = "https://api.open-meteo.com/v1/forecast"

# How many days the strip shows. Open-Meteo serves up to 16; ten is what fits
# a dashboard card without the columns becoming unreadable on a phone.
DAYS = 10

# How long a Ryan Hall read stays on the strip when it carries no explicit
# window. His long-form videos are about a specific setup a few days out, so a
# read with no window is treated as spent after three days rather than
# lingering as if it were a standing forecast.
READ_FALLBACK_TTL = timedelta(days=3)


# --------------------------------------------------------------------- icons

# WMO weather codes, grouped to the eight shapes worth distinguishing at this
# size. The full table has 28 values and most of the distinctions (light vs
# moderate drizzle) are invisible in a 24px icon, so they collapse.
#
# Inline SVG rather than emoji on purpose: emoji render at the mercy of
# whatever font the viewing device happens to have, and the two devices that
# matter here are a Linux desktop and an Android phone. A path always draws.
_SUN = '<circle cx="12" cy="12" r="5"/><g stroke-width="2" stroke-linecap="round">\
<path d="M12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3\
M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/></g>'
_CLOUD = '<path d="M7 18h10a4 4 0 0 0 .4-8 6 6 0 0 0-11.4 2A3.5 3.5 0 0 0 7 18z"/>'
_PARTLY = ('<circle cx="8" cy="8" r="3.2"/><g stroke-width="1.6" stroke-linecap="round">'
           '<path d="M8 2v2M8 12v2M2 8h2M12 8h2M3.8 3.8l1.4 1.4M10.8 10.8l1.4 1.4"/></g>'
           '<path d="M9 19h8a3.4 3.4 0 0 0 .3-6.8A5 5 0 0 0 8 13.5 2.9 2.9 0 0 0 9 19z"/>')
_FOG = ('<path d="M7 14h10a3.6 3.6 0 0 0 .4-7.2A5.5 5.5 0 0 0 6.4 8.6 3.2 3.2 0 0 0 7 14z"/>'
        '<g stroke-width="1.8" stroke-linecap="round"><path d="M4 17.5h16M7 20.5h11"/></g>')
_DRIZZLE = (_CLOUD + '<g stroke-width="1.8" stroke-linecap="round">'
            '<path d="M9 20v1.5M13 20v1.5M17 20v1.5"/></g>')
_RAIN = (_CLOUD + '<g stroke-width="1.8" stroke-linecap="round">'
         '<path d="M8.5 20l-1 3M12.5 20l-1 3M16.5 20l-1 3"/></g>')
_SNOW = (_CLOUD + '<g stroke-width="1.8" stroke-linecap="round">'
         '<path d="M9 21h.01M13 21h.01M17 21h.01M11 23h.01M15 23h.01"/></g>')
_STORM = (_CLOUD + '<path d="M13 19l-3 4h3l-1 3 4-5h-3l1-2z" stroke-width="1.4"/>')

_ICONS = {
    "sun": _SUN, "partly": _PARTLY, "cloud": _CLOUD, "fog": _FOG,
    "drizzle": _DRIZZLE, "rain": _RAIN, "snow": _SNOW, "storm": _STORM,
}

# code -> (icon key, spoken label)
_CODES = {
    0: ("sun", "Clear"), 1: ("sun", "Mostly clear"),
    2: ("partly", "Partly cloudy"), 3: ("cloud", "Overcast"),
    45: ("fog", "Fog"), 48: ("fog", "Freezing fog"),
    51: ("drizzle", "Light drizzle"), 53: ("drizzle", "Drizzle"),
    55: ("drizzle", "Heavy drizzle"),
    56: ("drizzle", "Freezing drizzle"), 57: ("drizzle", "Freezing drizzle"),
    61: ("rain", "Light rain"), 63: ("rain", "Rain"), 65: ("rain", "Heavy rain"),
    66: ("rain", "Freezing rain"), 67: ("rain", "Freezing rain"),
    71: ("snow", "Light snow"), 73: ("snow", "Snow"), 75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"),
    80: ("rain", "Showers"), 81: ("rain", "Showers"), 82: ("rain", "Violent showers"),
    85: ("snow", "Snow showers"), 86: ("snow", "Snow showers"),
    95: ("storm", "Thunderstorms"),
    96: ("storm", "Thunderstorms with hail"), 99: ("storm", "Thunderstorms with hail"),
}

# Codes that mean "a storm", for the summary sentence.
STORM_CODES = {95, 96, 99}


def describe(code) -> tuple[str, str]:
    """(icon key, label) for a WMO code. Unknown codes degrade to a cloud
    rather than raising — a forecast that renders with one odd icon is far
    better than a blank card because Open-Meteo added a code."""
    try:
        return _CODES[int(code)]
    except (TypeError, ValueError, KeyError):
        return ("cloud", "—")


def icon_svg(key: str, size: int = 26) -> str:
    body = _ICONS.get(key, _CLOUD)
    return (f'<svg viewBox="0 0 24 26" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" '
            f'aria-hidden="true">{body}</svg>')


# ----------------------------------------------------------------- the fetch

def fetch(lat: float, lon: float, *, user_agent: str, timeout: int = 20) -> dict:
    """Current conditions plus `DAYS` of daily forecast, in imperial units.

    `timezone=auto` makes Open-Meteo bucket the daily values by LOCAL midnight.
    Without it the "days" are UTC days, which near enough to Kentucky means a
    high that belongs to Tuesday afternoon lands on Wednesday's column.
    """
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "temperature_2m,apparent_temperature,weather_code,"
                   "wind_speed_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": str(DAYS),
    })
    req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------ the Ryan layer

def latest_read(con, *, now: datetime | None = None) -> dict | None:
    """The most recent Ryan Hall extraction that is still worth showing.

    Two filters, both of which exist because the alternative is a dashboard
    that states something false with total confidence:

      - It must have cleared the geofence (`fence_score` is stored on the row
        by layer 2). A read about the Dakotas is not a read about this house.
      - It must still be live: inside `window_end` if the extraction named a
        window, otherwise younger than READ_FALLBACK_TTL.

    Returns None when there is nothing current, and the caller renders "no
    current read" — which is a true statement, where a three-week-old severe
    weather summary rendered in the present tense is not.
    """
    now = now or datetime.now(timezone.utc)
    rows = con.execute(
        "SELECT * FROM extraction WHERE fence_score >= ? "
        "ORDER BY created_at DESC LIMIT 20", (geo.FENCE_THRESHOLD,)).fetchall()
    for row in rows:
        end = _parse_ts(row["window_end"])
        if end is not None:
            if end >= now:
                return _read_dict(row)
            continue
        created = _parse_ts(row["created_at"])
        if created is not None and now - created <= READ_FALLBACK_TTL:
            return _read_dict(row)
    return None


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Rows written before the code was consistent about tzinfo exist; treating
    # a naive timestamp as UTC matches how db.py stores them.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_dict(row) -> dict:
    def _list(raw):
        try:
            value = json.loads(raw or "[]")
            return [str(v) for v in value] if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []

    return {
        "summary": (row["summary"] or "").strip(),
        "hazards": _list(row["hazards"]),
        "confidence": (row["confidence"] or "").strip(),
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "video_id": row["video_id"],
    }


# -------------------------------------------------------------- the sentence

def summary_line(daily: dict) -> str:
    """One plain sentence about the ten days, derived rather than invented.

    Deliberately dull and deterministic: it names the next day with storms or
    a high chance of rain, or says there isn't one. Anything cleverer would be
    a forecast of our own, and this module is not qualified to make one.
    """
    codes = daily.get("weather_code") or []
    pops = daily.get("precipitation_probability_max") or []
    names = [_day_name(d, index) for index, d in enumerate(daily.get("time") or [])]

    for index, code in enumerate(codes):
        if _as_int(code) in STORM_CODES:
            return f"Storms showing up {_when(names, index)}."
    for index, pop in enumerate(pops):
        if _as_int(pop, 0) >= 60:
            return f"Wet {_when(names, index)} — {_as_int(pop, 0)}% chance."
    highest = max((_as_int(p, 0) for p in pops), default=0)
    if highest >= 30:
        return f"Nothing organised in the next {len(codes)} days; {highest}% is the wettest it gets."
    return f"Dry and settled through the next {len(codes)} days."


def _when(names: list[str], index: int) -> str:
    if index == 0:
        return "today"
    if index == 1:
        return "tomorrow"
    return names[index] if index < len(names) else f"in {index} days"


def _as_int(value, default: int | None = None) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _day_name(iso: str, index: int) -> str:
    if index == 0:
        return "Today"
    try:
        return datetime.fromisoformat(str(iso)).strftime("%a")
    except ValueError:
        return "—"


def _compass(degrees) -> str:
    value = _as_int(degrees)
    if value is None:
        return ""
    points = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return points[int((value % 360) / 22.5 + 0.5) % 16]


# ----------------------------------------------------------------- rendering

# Transparent background on purpose: the strip is iframed INTO a Homepage
# service card that already draws the frosted panel, so painting our own would
# put a second box inside the first one.
_CSS = """
*{box-sizing:border-box}
html,body{margin:0;background:transparent;color:#e2e8f0;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.strip{padding:.5rem .7rem;display:flex;flex-direction:column;gap:.55rem}
.now{display:flex;align-items:center;gap:.7rem;flex-wrap:wrap}
.now .temp{font-size:1.9rem;font-weight:600;line-height:1}
.now .cond{font-size:.85rem;color:#cbd5e1}
.now .i{color:#cbd5e1;display:flex;align-items:center}
.now .meta{margin-left:auto;font-size:.75rem;color:#94a3b8;
  display:flex;gap:.8rem;flex-wrap:wrap}
.read{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;
  font-size:.78rem;line-height:1.45;
  border-left:2px solid rgba(129,140,248,.6);padding-left:.6rem}
.read .who{font-weight:600;color:#c7d2fe;white-space:nowrap}
.read .tag{font-size:.62rem;letter-spacing:.09em;text-transform:uppercase;
  color:#94a3b8;border:1px solid rgba(226,232,240,.16);
  border-radius:999px;padding:.05rem .4rem}
.read .quiet{color:#94a3b8;font-style:italic}
.say{font-size:.82rem;color:#cbd5e1}
.ten{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:.25rem}
/* `> div` matters: `.ten div` would box the day label, the icon and each
   temperature separately instead of making one tile per day. */
.ten > div{text-align:center;font-size:.68rem;color:#94a3b8;
  background:rgba(2,6,23,.35);border:1px solid rgba(226,232,240,.055);
  border-radius:9px;padding:.35rem .1rem .4rem}
.ten .d{letter-spacing:.06em;text-transform:uppercase}
.ten .i{color:#cbd5e1;margin:.2rem 0;display:flex;justify-content:center}
.ten .hi{color:#e2e8f0;font-weight:600;font-size:.76rem}
.err{font-size:.82rem;color:#fca5a5;line-height:1.5}
.stamp{font-size:.62rem;color:#64748b}
/* The ten-day grid cannot hold ten readable columns on a 390px phone, so it
   becomes five by two rather than ten unreadable slivers. */
@media (max-width:640px){
  .ten{grid-template-columns:repeat(5,minmax(0,1fr))}
  .now .temp{font-size:1.6rem}
  .now .meta{margin-left:0;width:100%}
}
"""


def _page(body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>Weather strip</title><style>{_CSS}</style></head>"
            f"<body><div class=\"strip\">{body}</div></body></html>")


def render_error(message: str, *, now: datetime) -> str:
    """The page shown when the forecast could not be fetched. It says so."""
    stamp = now.astimezone().strftime("%a %-d %b, %-I:%M %p")
    return _page(
        f'<div class="err">The forecast could not be fetched.<br>'
        f'{html.escape(message)}</div>'
        f'<div class="stamp">Last attempt {html.escape(stamp)}. '
        f'Nothing stale is being shown in its place.</div>')


def render(data: dict, read: dict | None, *, now: datetime) -> str:
    current = data.get("current") or {}
    daily = data.get("daily") or {}

    code = current.get("weather_code")
    icon_key, label = describe(code)
    temp = _as_int(current.get("temperature_2m"))
    feels = _as_int(current.get("apparent_temperature"))
    wind = _as_int(current.get("wind_speed_10m"))
    gust_dir = _compass(current.get("wind_direction_10m"))

    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    today_hi, today_lo = _as_int(highs[0] if highs else None), _as_int(lows[0] if lows else None)

    meta = []
    if today_lo is not None and today_hi is not None:
        meta.append(f"&darr;{today_lo}&deg; &uarr;{today_hi}&deg;")
    if feels is not None and temp is not None and abs(feels - temp) >= 3:
        meta.append(f"feels {feels}&deg;")
    if wind is not None:
        meta.append(f"{gust_dir} {wind} mph".strip())

    now_html = (
        f'<div class="now"><span class="i">{icon_svg(icon_key, 34)}</span>'
        f'<span class="temp">{"—" if temp is None else temp}&deg;</span>'
        f'<span class="cond">{html.escape(label)}</span>'
        f'<span class="meta">{"".join(f"<span>{m}</span>" for m in meta)}</span></div>')

    ten_html = '<div class="ten">' + "".join(
        f'<div><div class="d">{html.escape(_day_name(day, index))}</div>'
        f'<div class="i">{icon_svg(describe(day_code)[0], 22)}</div>'
        f'<div class="hi">{_fmt(highs, index)}&deg;</div>'
        f'<div>{_fmt(lows, index)}&deg;</div></div>'
        for index, (day, day_code) in enumerate(
            zip(daily.get("time") or [], daily.get("weather_code") or []))
    ) + "</div>"

    stamp = now.astimezone().strftime("%-I:%M %p")
    return _page(
        now_html
        + _read_html(read)
        + f'<div class="say">{html.escape(summary_line(daily))}</div>'
        + ten_html
        + f'<div class="stamp">Open-Meteo, updated {html.escape(stamp)}</div>')


def _fmt(values, index) -> str:
    value = _as_int(values[index]) if index < len(values) else None
    return "—" if value is None else str(value)


def _read_html(read: dict | None) -> str:
    if not read:
        # Said plainly rather than hidden. An empty space where the Ryan Hall
        # line usually sits reads as a broken card; this reads as a quiet week.
        return ('<div class="read"><span class="who">Ryan Hall</span>'
                '<span class="quiet">no current read for this area</span></div>')

    bits = [f'<span class="who">Ryan Hall</span>']
    if read["confidence"]:
        bits.append(f'<span class="tag">{html.escape(read["confidence"])}</span>')
    for hazard in read["hazards"][:3]:
        bits.append(f'<span class="tag">{html.escape(hazard)}</span>')
    summary = read["summary"] or "a setup worth watching"
    bits.append(f'<span>{html.escape(summary)}</span>')
    return '<div class="read">' + "".join(bits) + "</div>"


def build(con, lat: float, lon: float, *, user_agent: str,
          now: datetime | None = None) -> str:
    """Fetch, read and render. Never raises for an upstream failure — it
    renders the failure instead, so the card always says something true."""
    now = now or datetime.now(timezone.utc)
    try:
        data = fetch(lat, lon, user_agent=user_agent)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return render_error(str(exc), now=now)
    return render(data, latest_read(con, now=now), now=now)
