"""The briefing card's HTML.

One wide card on the dashboard's Home tab. It answers, in this order:
is anything wrong, what is today about, and — always — here is something worth
reading. Chris's framing was the switchboard's: *"there are some issues"*,
falling back to something tempting when there are not.

⚠️ Links open in a NEW TAB (`target=_blank`). Inside an iframe a bare link
navigates the FRAME, which would leave a dashboard card showing a full news
site. `_top` would work too but throws away the dashboard.

⚠️ The Homepage iframe widget sets `pointer-events: none` on the frame when
`allowScrolling: "no"` is configured. This card is meant to be clicked, so its
service entry must NOT set that — see modules/services/homepage.nix.
"""
from __future__ import annotations

import html
import re
from datetime import datetime

CSS = """
*{box-sizing:border-box}
/* ⚠️ overflow:hidden is not cosmetic. This page is an iframe inside a card;
   when the content runs a few pixels past the frame the browser puts a
   SCROLLBAR inside the card, which looks broken. Measured 2026-10-19: 197px
   of content in a 190px frame did exactly that. The frame height in
   homepage.nix carries the headroom; this makes a near-miss impossible to
   see. */
html,body{margin:0;height:100%;overflow:hidden;background:transparent;color:#e2e8f0;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
/* container-type lets the breakpoint below measure THIS ELEMENT rather than
   the viewport. Inside an iframe a `width=device-width` viewport meta can
   resolve to the device's width rather than the frame's, so a plain media
   query is not reliably about the space the card actually has. The media
   query is kept as the fallback for anything without container support. */
.wrap{padding:.6rem .75rem;display:flex;flex-direction:column;gap:.6rem;height:100%;
  container-type:inline-size}

.status{display:flex;align-items:center;gap:.5rem;font-size:.72rem;
  letter-spacing:.1em;text-transform:uppercase;color:#94a3b8}
.dot{width:.5rem;height:.5rem;border-radius:999px;flex:none}
.dot.ok{background:#34d399;box-shadow:0 0 0 3px rgba(52,211,153,.15)}
.dot.warn{background:#fbbf24;box-shadow:0 0 0 3px rgba(251,191,36,.15)}

/* One wrapping row of pills, not a stack of rows: the number of open
   conditions must not change the height of the card. */
.issues{display:flex;flex-wrap:wrap;gap:.3rem}
.issue{display:inline-flex;gap:.4rem;align-items:baseline;font-size:.74rem;
  background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);
  border-radius:999px;padding:.2rem .6rem;white-space:nowrap}
.issue .n{font-weight:600;color:#fde68a}
.issue .age{color:#94a3b8;font-size:.66rem}
.issue.more{background:none;border-color:rgba(226,232,240,.12);color:#94a3b8}

.cols{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;flex:1;min-height:0}
.panel{background:rgba(2,6,23,.32);border:1px solid rgba(226,232,240,.06);
  border-radius:11px;padding:.55rem .7rem;display:flex;flex-direction:column;gap:.3rem;
  min-height:0;overflow:hidden}
.kicker{font-size:.62rem;letter-spacing:.12em;text-transform:uppercase;color:#94a3b8}
.plan{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:.28rem;
  min-height:0;overflow:hidden}
.plan li{font-size:.78rem;line-height:1.35;color:#cbd5e1;
  padding-left:.75rem;position:relative;
  overflow:hidden;text-overflow:ellipsis;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical}
.plan li::before{content:"";position:absolute;left:0;top:.5rem;
  width:.25rem;height:.25rem;border-radius:999px;background:rgba(129,140,248,.8)}

/* The read panel is the one thing here meant to be inviting rather than
   merely legible, so it gets the larger type and the only hover state. */
.read{justify-content:space-between;
  background:linear-gradient(135deg,rgba(129,140,248,.14),rgba(13,148,136,.10));
  border-color:rgba(129,140,248,.22);transition:border-color .18s ease,transform .18s ease}
.read .tldr{font-size:.92rem;line-height:1.4;color:#f1f5f9;font-weight:500;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
.read .go{font-size:.7rem;color:#c7d2fe;display:flex;gap:.3rem;align-items:center}
@media (hover:hover){
  .read:hover{border-color:rgba(129,140,248,.55);transform:translateY(-1px)}
}
.read.solo{grid-column:1 / -1}
.quiet{font-size:.78rem;color:#94a3b8;font-style:italic}

@media (max-width:640px){
  .cols{grid-template-columns:1fr}
  .read .tldr{font-size:.86rem;-webkit-line-clamp:2}
}
@container (max-width:560px){
  .cols{grid-template-columns:1fr}
  .read .tldr{font-size:.86rem;-webkit-line-clamp:2}
}
"""

# How many open conditions are listed before the rest are summarised. Three
# is what fits without the card turning into an incident log — there is a
# whole page for that, and it is linked.
MAX_ISSUES = 3


def _page(body: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Briefing</title><style>{CSS}</style></head>'
            f'<body><div class="wrap">{body}</div></body></html>')


def _age(days) -> str:
    if days is None:
        return ""
    if days < 1:
        return "today"
    if days < 2:
        return "1 day"
    if days < 14:
        return f"{int(days)} days"
    return f"{int(days // 7)} weeks"


def _status(issues: list[dict], sentinel_url: str) -> str:
    if not issues:
        return ('<div class="status"><span class="dot ok"></span>'
                '<span>Nothing needs attention</span></div>')
    count = len(issues)
    noun = "thing wants" if count == 1 else "things want"
    return (f'<a class="status" href="{html.escape(sentinel_url)}" '
            f'target="_blank" rel="noopener">'
            f'<span class="dot warn"></span>'
            f'<span>{count} {noun} attention</span></a>')


def _issues_html(issues: list[dict]) -> str:
    if not issues:
        return ""
    rows = []
    for issue in issues[:MAX_ISSUES]:
        age = _age(issue.get("age_days"))
        detail = useful_detail(issue.get("detail"))
        # The raw evidence line goes in a tooltip rather than on the face of
        # the pill — it is the difference between a readable card and a row of
        # Prometheus expressions.
        title = f' title="{html.escape(detail)}"' if detail else ""
        rows.append(
            f'<span class="issue"{title}>'
            f'<span class="n">{html.escape(issue["label"])}</span>'
            + (f'<span class="age">{html.escape(age)}</span>' if age else "")
            + '</span>')
    remaining = len(issues) - MAX_ISSUES
    if remaining > 0:
        rows.append(f'<span class="issue more">and {remaining} more</span>')
    return '<div class="issues">' + "".join(rows) + "</div>"


def useful_detail(raw) -> str:
    """Strip a sentinel evidence line down to prose, or to nothing.

    Evidence first lines come in two shapes. Some are written for a human
    ("nginx is 'failed' (expected active) — EVERY vhost on the box is
    unreachable"); others are a bare threshold expression, and they arrive
    with the check id repeated twice in front of them:

        [swap-headroom] swap-headroom: 127.0.0.1:9100=7.9621

    Showing that on a dashboard is worse than showing nothing, so the id
    prefixes are removed and what is left has to look like a sentence — it
    must contain a space and have some length — before it is used at all.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"^[a-z0-9][a-z0-9-]*:\s*", "", text)
    text = text.strip()
    if len(text) < 12 or " " not in text:
        return ""
    return text


def _plan_html(plan: list[str], notes_url: str) -> str:
    if not plan:
        return ""
    items = "".join(f"<li>{html.escape(item)}</li>" for item in plan)
    return ('<a class="panel" href="' + html.escape(notes_url) + '" '
            'target="_blank" rel="noopener">'
            '<div class="kicker">Today</div>'
            f'<ul class="plan">{items}</ul></a>')


def _read_html(ed: dict | None, solo: bool) -> str:
    classes = "panel read solo" if solo else "panel read"
    if not ed:
        # Nothing publishable today. Said plainly — an empty panel would read
        # as a broken card, and inventing a teaser would be worse.
        return (f'<div class="{classes}"><div class="kicker">Worth reading</div>'
                '<div class="quiet">No edition today.</div></div>')
    count = ed.get("published") or 0
    return (f'<a class="{classes}" href="{html.escape(ed["url"])}" '
            f'target="_blank" rel="noopener">'
            '<div class="kicker">Worth reading</div>'
            f'<div class="tldr">{html.escape(ed["tldr"])}</div>'
            f'<div class="go">Today\'s edition &middot; {count} '
            f'{"story" if count == 1 else "stories"} &rarr;</div></a>')


def render(issues: list[dict], plan: list[str], ed: dict | None, *,
           notes_url: str, sentinel_url: str, now: datetime | None = None) -> str:
    """Build the card.

    Layout, in Chris's priority order: the status line always, open issues when
    there are any, then two panels — what today is about, and what to read. If
    there is no plan for today the read panel takes the whole width, which is
    the "if nothing there, give me something to read" case.
    """
    plan_html = _plan_html(plan, notes_url)
    body = (_status(issues, sentinel_url)
            + _issues_html(issues)
            + '<div class="cols">'
            + plan_html
            + _read_html(ed, solo=not plan_html)
            + '</div>')
    return _page(body)
