"""Notice a sustained bitcoin price spike and, only then, find out why.

Chris, 2026-09-18: BTC sometimes jumps a few percent over an hour or two and
he doesn't want to go digging for the reason. The cost worry is real, so the
model is NEVER called on a schedule — a cheap arithmetic detector runs on a
timer over a self-maintained price log, and `claude -p` (which can web-search)
is invoked ONLY when a sustained move clears the threshold and it's a genuinely
new leg, not a plateau we've already explained.

Flow (cli `btc-watch`, on switchboard-btcwatch.timer):
  1. sample the current price (mempool /api/v1/prices) -> append to
     <state>/btc-history.json, prune > history_keep_s.
  2. detect(): over the trailing window, the sustained move from the window's
     low (for an up-spike) or high (down) to now, requiring the move to have
     been RETAINED (>= sustain_frac of it still present) so a reverted blip
     doesn't count.
  3. fire only if it's a new leg vs the last explained reference (a further
     threshold move in the same direction, a direction flip, or the prior
     explanation aged out) -> one claude -p call -> store as the "btc-move"
     standing answer + one quiet-hours-respecting ntfy.

The phone reads it back via the "btc-move" intent ("why did bitcoin move").
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .sources import SourceError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- price log

def _history_path(settings: Settings) -> Path:
    return settings.state_dir / "btc-history.json"


def load_history(settings: Settings) -> list[tuple[float, float]]:
    try:
        return [(float(t), float(p)) for t, p in json.loads(_history_path(settings).read_text())]
    except (OSError, ValueError, TypeError):
        return []


def save_history(settings: Settings, hist: list[tuple[float, float]]) -> None:
    p = _history_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.part")
    tmp.write_text(json.dumps([[round(t), round(pr, 2)] for t, pr in hist]))
    tmp.replace(p)


async def sample_price(settings: Settings) -> tuple[float, float]:
    """(feed_timestamp, usd) from the local mempool backend."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.mempool_url}/api/v1/prices")
        resp.raise_for_status()
        d = resp.json()
        return float(d["time"]), float(d["USD"])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise SourceError(f"mempool prices: {exc}") from exc


# ---------------------------------------------------------------- detection

@dataclass(frozen=True)
class Move:
    direction: str      # "up" | "down"
    pct: float          # magnitude of the sustained move, always positive
    lo: float
    hi: float
    now: float
    minutes: float      # span from the extreme-start to now


import math


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _hourly(hist: list[tuple[float, float]], now_ts: float, hours: int) -> list[float]:
    """Newest price in each hourly bucket over the last `hours`, oldest->newest.
    Buckets the dense 10-min samples so sigma/moves match the hourly backtest."""
    b: dict[int, float] = {}
    for t, pr in sorted(hist):
        if t > now_ts or t < now_ts - hours * 3600:
            continue
        b[int((now_ts - t) // 3600)] = pr        # later t in a bucket wins (newest)
    return [b[i] for i in sorted(b, reverse=True)]  # largest index = oldest


def detect(hist: list[tuple[float, float]], now_ts: float, *, window_h: int,
           sigma_k: float, min_move_pct: float, min_hours: int) -> "Move | None":
    """A move that is BOTH a statistical outlier vs the last 24h of churn AND a
    real economic shift of the mean. Regime-adaptive: the bar rises with
    volatility, so it stays quiet in a bull run's routine chop yet still fires
    on genuine legs (validated across calm/grind/drop/crash months, 2026-09-18).

      sigma  = stdev of hourly log-returns over the trailing ~24h
      z      = (W-hour log-return) / (sigma * sqrt(W))     -> outlier test
      disp   = (mean(last 2h) - mean(prior hours)) / mean  -> "did it really move"
      fire   = |z| >= sigma_k AND |disp| >= min_move_pct AND signs agree
    """
    hp = _hourly(hist, now_ts, 25)
    if len(hp) < max(min_hours, window_h + 3):
        return None
    rets = [math.log(hp[j] / hp[j - 1]) for j in range(1, len(hp)) if hp[j - 1] > 0 and hp[j] > 0]
    sigma = _stdev(rets)
    if sigma <= 0:
        return None
    now = hp[-1]
    then = hp[-1 - window_h]
    if then <= 0:
        return None
    recent = math.log(now / then)
    z = recent / (sigma * math.sqrt(window_h))
    base = hp[:-window_h]
    mean_base = sum(base) / len(base)
    mean_recent = sum(hp[-2:]) / 2
    disp = (mean_recent - mean_base) / mean_base if mean_base else 0.0
    if abs(z) < sigma_k or abs(disp) * 100 < min_move_pct or (recent > 0) != (disp > 0):
        return None
    pct = round((now - then) / then * 100, 2)
    if recent > 0:
        return Move("up", abs(pct), then, now, now, window_h * 60)
    return Move("down", abs(pct), now, then, now, window_h * 60)


def is_new_leg(move: Move, last: dict | None, *, threshold_pct: float, reexplain_after_s: float, now_ts: float) -> bool:
    """Should this move trigger a (costly) explanation? Keeps a plateau quiet."""
    if last is None:
        return True
    if last.get("direction") != move.direction:
        return True                                   # reversal is always news
    ref = float(last.get("reference_price") or 0)
    if ref and abs(move.now - ref) / ref * 100 >= threshold_pct:
        return True                                   # another full leg beyond what we explained
    if now_ts - float(last.get("ts") or 0) >= reexplain_after_s:
        return True                                   # the explanation is stale and a move still stands
    return False


# ---------------------------------------------------------------- explain + store

_PROMPT = """\
Bitcoin has moved {dir_word} {pct:.1f} percent, from about {lo} dollars to about \
{now} dollars, over roughly the last {minutes} minutes (as of {when}). Search the \
web and recent news for the most likely reason. Answer in two or three short \
spoken sentences, plain prose, no markdown or URLs: lead with the catalyst, then \
any confirming detail. If you genuinely cannot find a reason, say the move \
happened and that there's no clear catalyst in the news yet."""


def _dollars(n: float) -> str:
    return f"{round(n):,}"


def explain(settings: Settings, move: Move) -> str:
    when = time.strftime("%-I:%M %p", time.localtime())
    prompt = _PROMPT.format(dir_word=("up" if move.direction == "up" else "down"), pct=move.pct,
                            lo=_dollars(move.lo if move.direction == "up" else move.hi),
                            now=_dollars(move.now), minutes=int(move.minutes), when=when)
    proc = subprocess.run(
        [settings.claude_bin, "-p", prompt, "--output-format", "text"],
        cwd=str(settings.claude_cwd), capture_output=True, text=True,
        env={**_env(), "CLAUDE_AUTONOMOUS": "1"}, timeout=settings.btc_explain_timeout_s,
    )
    if proc.returncode != 0:
        raise SourceError(f"claude -p exited {proc.returncode}: {proc.stderr[-300:]}")
    text = _clean_answer(proc.stdout)
    if not text:
        raise SourceError("claude -p returned nothing")
    return text


import re as _re


def _clean_answer(raw: str) -> str:
    """Strip the model's citation tail and any markup — Kokoro must not read
    URLs aloud. Cuts from a 'Sources:'/'Source:' marker, unwraps [text](url),
    drops bare URLs and markdown emphasis."""
    text = _re.split(r"\n?\s*Sources?\s*:", raw, maxsplit=1)[0]
    text = _re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)   # [label](url) -> label
    text = _re.sub(r"https?://\S+", "", text)
    text = text.replace("**", "").replace("`", "").replace("*", "")
    return " ".join(text.split())


def _env() -> dict:
    import os
    e = dict(os.environ)
    e.setdefault("HOME", "/home/claude")
    return e


def spoken(answer: dict, now_ts: float | None = None) -> str:
    now_ts = now_ts or time.time()
    age_h = (now_ts - float(answer.get("ts", now_ts))) / 3600
    when = "just now" if age_h < 1 else ("about an hour ago" if age_h < 1.5 else f"about {round(age_h)} hours ago")
    return f"{answer['text']} That was {when}."


# ---------------------------------------------------------------- the timer entry point

def run(settings: Settings, now_ts: float | None = None) -> dict:
    """One watch tick. Returns a small status dict (also what the CLI prints)."""
    now_ts = now_ts or time.time()
    import asyncio
    hist = load_history(settings)
    try:
        _feed_ts, price = asyncio.run(sample_price(settings))
    except SourceError as exc:
        log.warning("btc-watch: %s", exc)
        return {"ok": False, "error": str(exc)}
    # de-dupe identical feed samples (the backend only refreshes every few min)
    if not hist or abs(hist[-1][1] - price) > 0.001 or now_ts - hist[-1][0] >= settings.btc_sample_min_gap_s:
        hist.append((now_ts, price))
    hist = [(t, p) for t, p in hist if t >= now_ts - settings.btc_history_keep_s]
    save_history(settings, hist)

    move = detect(hist, now_ts, window_h=settings.btc_window_h, sigma_k=settings.btc_sigma_k,
                  min_move_pct=settings.btc_min_move_pct, min_hours=settings.btc_min_hours)
    if move is None:
        return {"ok": True, "price": price, "move": None}

    last = _load_move(settings)
    if not is_new_leg(move, last, threshold_pct=settings.btc_min_move_pct,
                      reexplain_after_s=settings.btc_reexplain_after_s, now_ts=now_ts):
        return {"ok": True, "price": price, "move": f"{move.direction} {move.pct}%", "explained": "already"}

    log.info("btc-watch: %s %.1f%% (%s->%s over %dm) — explaining", move.direction, move.pct,
             _dollars(move.lo), _dollars(move.now), int(move.minutes))
    try:
        text = explain(settings, move)
    except SourceError as exc:
        log.warning("btc-watch explain: %s", exc)
        return {"ok": False, "error": str(exc), "move": f"{move.direction} {move.pct}%"}
    _store_move(settings, move, text, now_ts)
    _notify(settings, move, text)
    return {"ok": True, "price": price, "move": f"{move.direction} {move.pct}%", "explained": "new", "text": text[:120]}


def _move_path(settings: Settings) -> Path:
    return settings.answers_dir / "btc-move.json"


def _load_move(settings: Settings) -> dict | None:
    try:
        return json.loads(_move_path(settings).read_text())
    except (OSError, ValueError):
        return None


def _store_move(settings: Settings, move: Move, text: str, now_ts: float) -> None:
    settings.answers_dir.mkdir(parents=True, exist_ok=True)
    d = {"text": text, "ts": now_ts, "direction": move.direction, "pct": move.pct,
         "reference_price": move.now, "lo": move.lo, "hi": move.hi}
    p = _move_path(settings)
    tmp = p.with_suffix(".json.part")
    tmp.write_text(json.dumps(d))
    tmp.replace(p)


def _notify(settings: Settings, move: Move, text: str) -> None:
    """One informational ntfy — never overnight (quiet hours), never wakes anyone."""
    if not settings.btc_notify:
        return
    hour = int(time.strftime("%H"))
    if hour >= settings.quiet_start_h or hour < settings.quiet_end_h:
        log.info("btc-watch: quiet hours, holding the ntfy")
        return
    arrow = "up" if move.direction == "up" else "down"
    title = f"Bitcoin {arrow} {move.pct:.1f}% to ${_dollars(move.now)}"
    try:
        httpx.post(settings.ntfy_post_url, content=text.encode("ascii", "replace"),
                   headers={"Title": title.encode("ascii", "replace").decode(), "Priority": "default", "Tags": "chart_with_upwards_trend"},
                   timeout=5.0)
    except httpx.HTTPError as exc:
        log.warning("btc-watch ntfy: %s", exc)
