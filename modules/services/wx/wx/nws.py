"""LAYER 1 — official NWS alerts for one point. The only layer allowed to wake him.

The query is `alerts/active?point=<lat>,<lon>`, which NWS resolves against the
actual warning POLYGON rather than the county. That matters: Owen County is
large and a warning clipped to its eastern third is not about his house.

WHAT MAY WAKE HIM is deliberately short and matches the standing rule — only a
physical danger to the household justifies piercing 22:00-07:00. Chris signed
off on this exact list on 2026-09-07 and was explicit that a Severe
Thunderstorm Warning is NOT on it.

⚠️ DEDUP IS THE REAL WORK HERE. NWS reissues and updates constantly, each time
with a NEW alert id. Notifying per id turns one storm into fifteen pushes,
which is how an alert channel gets muted forever — and a muted channel is worse
than no channel, because it looks like it is working.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.weather.gov/alerts/active"

# Pierces quiet hours. Physical danger only.
CRITICAL_EVENTS = {
    "Tornado Warning",
    "Flash Flood Emergency",       # not a distinct event type; see classify()
    "Extreme Wind Warning",
}

# Real, worth knowing, held until 07:00 if it is the middle of the night.
WARNING_EVENTS = {
    "Severe Thunderstorm Warning",
    "Flash Flood Warning",
    "Ice Storm Warning",
    "Winter Storm Warning",
    "Blizzard Warning",
    "High Wind Warning",
    "Fire Weather Warning",
    "Tornado Watch",
    "Severe Thunderstorm Watch",
}

# Ordered ranks so an upgrade can be told from a reissue.
CLASS_RANK = {"info": 0, "warning": 1, "critical": 2}

# Two alerts for the same hazard family inside this window are treated as the
# same episode unless the class went UP.
REISSUE_COOLDOWN = timedelta(hours=2)


def family(event: str) -> str:
    """'Tornado Warning' -> 'Tornado'. The unit of "same storm, again"."""
    out = event
    for suffix in (" Warning", " Watch", " Advisory", " Statement", " Emergency"):
        if out.endswith(suffix):
            out = out[: -len(suffix)]
    return out.strip()


def classify(props: dict) -> str:
    """critical (wakes him) | warning (held to 07:00) | info (never pushed)."""
    event = (props.get("event") or "").strip()

    # A Flash Flood Emergency arrives as a Flash Flood Warning carrying
    # severity=Extreme and 'FLASH FLOOD EMERGENCY' in the description. It is
    # the most dangerous product the office issues and it is not its own event
    # type, so matching on the event name alone would miss it entirely.
    blob = " ".join(str(props.get(k) or "") for k in ("headline", "description", "event")).upper()
    if "FLASH FLOOD EMERGENCY" in blob or "PARTICULARLY DANGEROUS SITUATION" in blob:
        return "critical"

    if event in CRITICAL_EVENTS:
        return "critical"
    if (props.get("severity") or "") == "Extreme":
        return "critical"
    if event in WARNING_EVENTS:
        return "warning"
    return "info"


def fetch(lat: float, lon: float, *, user_agent: str, timeout: int = 20) -> list[dict]:
    """Active alerts whose polygon contains the point.

    ⚠️ Coordinates go in the URL and nowhere else. They are never logged and
    never put in a notification body — see db.load_location.
    """
    url = f"{API}?point={lat},{lon}"
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,          # NWS requires a contactable UA
        "Accept": "application/geo+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return [f.get("properties") or {} for f in payload.get("features") or []]


def _recent_same_family(con: sqlite3.Connection, fam: str, cutoff: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM nws_alert"
        " WHERE notified_class IS NOT NULL AND notified_at >= ?"
        "   AND event LIKE ?"
        " ORDER BY notified_at DESC LIMIT 1",
        (cutoff, f"{fam}%")).fetchone()


def process(con: sqlite3.Connection, alerts: list[dict], *,
            when: datetime | None = None) -> list[dict]:
    """Record what is active; return only what he has not effectively been told.

    Returns dicts of {class, event, headline, key, title, body} — deciding
    whether to actually send is notify.py's job, because that is where the
    quiet-hours contract lives.
    """
    when = when or datetime.now(timezone.utc)
    cutoff = (when - REISSUE_COOLDOWN).isoformat()
    # ⚠️ Every timestamp written here comes from `when`, never from the wall
    # clock. Mixing the two is what the first version did, and it meant the
    # cooldown compared an injected cutoff against a real-time notified_at —
    # so the suppression window was whatever the gap between the two happened
    # to be. Silent, and only wrong under test or after a clock change.
    stamp = when.isoformat()
    out: list[dict] = []

    for props in alerts:
        aid = props.get("id")
        event = (props.get("event") or "").strip()
        if not aid or not event:
            continue

        cls = classify(props)
        seen = con.execute("SELECT * FROM nws_alert WHERE id = ?", (aid,)).fetchone()
        if seen is None:
            con.execute(
                "INSERT INTO nws_alert (id, event, severity, urgency, certainty,"
                " headline, area_desc, onset, ends, sent, first_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (aid, event, props.get("severity"), props.get("urgency"),
                 props.get("certainty"), props.get("headline"),
                 props.get("areaDesc"), props.get("onset"), props.get("ends"),
                 props.get("sent"), stamp))
        elif seen["notified_class"] is not None:
            # Already told him about this exact alert. Never twice.
            continue

        if cls == "info":
            continue

        fam = family(event)

        # Reissue suppression. An update to a live warning arrives as a NEW id
        # that `references` the old one; a routine continuation may not
        # reference anything at all, which is why the family cooldown is here
        # as well. Either way: only an ESCALATION gets through.
        prior = _recent_same_family(con, fam, cutoff)
        if prior is not None:
            if CLASS_RANK[cls] <= CLASS_RANK.get(prior["notified_class"] or "info", 0):
                # Same episode, same or lower class. Record it, stay quiet.
                con.execute("UPDATE nws_alert SET notified_class = ?, notified_at = ?"
                            " WHERE id = ?", (cls, stamp, aid))
                continue

        con.execute("UPDATE nws_alert SET notified_class = ?, notified_at = ?"
                    " WHERE id = ?", (cls, stamp, aid))
        out.append({
            "class": cls,
            "event": event,
            "key": f"nws:{fam}",
            "title": event,
            "body": (props.get("headline")
                     or props.get("description", "")[:280]
                     or event).strip(),
            "escalated": prior is not None,
        })

    con.commit()
    return out
