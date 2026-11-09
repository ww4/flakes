"""The project card's HTML — one row per project, for the Lab tab.

Same shape as the briefing card: transparent background (it sits inside a
Homepage tile that already draws the panel), links open in a new tab, and
nothing may be allowed to shrink below its own text height.

⚠️ `flex:none` on every row and every piece of a row. These are flex children
inside a column with overflow:hidden, and the default flex-shrink:1 squashes
them through the middle of their letters when the frame runs short — the
exact fault that made the briefing card unreadable on a phone on 2026-10-19.
"""
from __future__ import annotations

import html

CSS = """
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;background:transparent;color:#e2e8f0;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
.wrap{padding:.55rem .7rem;display:flex;flex-direction:column;gap:.4rem;height:100%;
  container-type:inline-size}

.head{flex:none;display:flex;align-items:center;gap:.5rem;font-size:.62rem;
  letter-spacing:.12em;text-transform:uppercase;color:#94a3b8}
.head .go{margin-left:auto;color:#c7d2fe;text-transform:none;letter-spacing:0;
  font-size:.7rem}

.rows{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;gap:.32rem}
.p{flex:none;display:block;background:rgba(2,6,23,.32);
  border:1px solid rgba(226,232,240,.06);border-radius:10px;padding:.4rem .55rem;
  transition:border-color .18s ease,transform .18s ease}
@media (hover:hover){.p:hover{border-color:rgba(129,140,248,.45);transform:translateY(-1px)}}

.top{flex:none;display:flex;align-items:baseline;gap:.5rem}
.name{flex:1;min-width:0;font-size:.8rem;font-weight:600;color:#e2e8f0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pct{flex:none;font-size:.74rem;color:#cbd5e1;font-variant-numeric:tabular-nums}
.chip{flex:none;font-size:.58rem;letter-spacing:.08em;text-transform:uppercase;
  border-radius:999px;padding:.08rem .4rem;border:1px solid}
.chip.blocked{color:#fde68a;border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.1)}
.chip.active{color:#a5b4fc;border-color:rgba(129,140,248,.35);background:rgba(129,140,248,.08)}
.chip.paused{color:#94a3b8;border-color:rgba(148,163,184,.3)}
.chip.done{color:#6ee7b7;border-color:rgba(52,211,153,.3);background:rgba(52,211,153,.08)}

/* The bar is the glance. Three pixels, full width, no label — the number is
   already on the row. */
.bar{flex:none;height:3px;border-radius:999px;background:rgba(226,232,240,.08);
  margin:.3rem 0 .25rem;overflow:hidden}
.bar i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,#818cf8,#22d3ee)}
.p.blocked .bar i{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.p.done .bar i{background:linear-gradient(90deg,#34d399,#6ee7b7)}

.next{flex:none;font-size:.7rem;line-height:1.3;color:#94a3b8;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.next b{color:#cbd5e1;font-weight:600}
.quiet{font-size:.78rem;color:#94a3b8;font-style:italic}

@container (max-width:560px){
  .name{font-size:.76rem}
  .next{font-size:.67rem}
}
"""

# How many rows fit before the card stops being a glance. Four, because every
# row is a fixed height (all three lines are single-line and ellipsised), so
# four rows is a card of exactly ~280px at every width — and a frame that
# cannot fit its contents is the bug that cost two rounds on the briefing
# card. The full board is one tap away and the header links to it.
MAX_ROWS = 4


def _page(body: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Projects</title><style>{CSS}</style></head>'
            f'<body><div class="wrap">{body}</div></body></html>')


def _row(project: dict, board_url: str) -> str:
    status = project["status"] if project["status"] in (
        "blocked", "active", "paused", "done") else "active"
    pct = max(0, min(100, int(project["percent"])))

    if project["next"]:
        label = "Blocked on" if status == "blocked" else "Next"
        nxt = (f'<div class="next"><b>{label}:</b> '
               f'{html.escape(project["next"])}</div>')
    else:
        nxt = ""

    steps = (f'{project["done"]}/{project["total"]}'
             if project["total"] else "no steps")

    return (
        f'<a class="p {status}" href="{html.escape(board_url)}" '
        f'target="_blank" rel="noopener">'
        '<div class="top">'
        f'<span class="name">{html.escape(project["title"])}</span>'
        f'<span class="chip {status}">{html.escape(status)}</span>'
        f'<span class="pct">{pct}% &middot; {steps}</span>'
        '</div>'
        f'<div class="bar"><i style="width:{pct}%"></i></div>'
        f'{nxt}</a>')


def render(projects: list[dict], *, board_url: str) -> str:
    if not projects:
        # Said plainly. An empty card reads as broken; this reads as a clear
        # desk, which is a real and good state.
        return _page('<div class="head"><span>Projects</span></div>'
                     '<div class="quiet">No projects on the board.</div>')

    shown = projects[:MAX_ROWS]
    more = len(projects) - len(shown)
    head = ('<div class="head"><span>Projects</span>'
            + (f'<span class="go">and {more} more &rarr;</span>' if more > 0
               else '<span class="go">Open the board &rarr;</span>')
            + '</div>')
    rows = "".join(_row(p, board_url) for p in shown)
    return _page(head + f'<div class="rows">{rows}</div>')
