import math
import time

from switchboard import btcwatch
from switchboard.config import Settings

H = 3600.0


def hourly(now, prices):
    """prices oldest->newest, one per hour ending at `now`."""
    n = len(prices)
    return [(now - (n - 1 - i) * H, float(p)) for i, p in enumerate(prices)]


def D(hist, now, **kw):
    d = dict(window_h=3, sigma_k=3.0, min_move_pct=1.2, min_hours=8)
    d.update(kw)
    return btcwatch.detect(hist, now, **d)


def test_today_up_spike_fires():
    now = 1_000_000.0
    # ~24h flat near 78k, then a clean run to 81k over 3h (the real 09-18 shape)
    flat = [78000, 78100, 77900, 78050, 78000, 78200, 78100, 77950, 78000, 78100,
            78050, 77900, 78000, 78150, 78050, 77980, 78000, 78100, 78050, 78000, 78100]
    spike = [79200, 80400, 81253]
    m = D(hourly(now, flat + spike), now)
    assert m and m.direction == "up" and m.pct > 3 and round(m.now) == 81253


def test_reverted_blip_does_not_fire():
    now = 1_000_000.0
    flat = [80000 + (50 if i % 2 else -50) for i in range(22)]
    blip = [83300, 80100, 80000]   # popped then came right back
    assert D(hourly(now, flat + blip), now) is None


def test_bull_run_routine_chop_is_quiet():
    now = 1_000_000.0
    # high-vol tape: +-2%/hr swings but no net directional leg beyond the churn
    import random
    random.seed(7)
    p = 100000.0
    prices = []
    for _ in range(28):
        p *= 1 + random.uniform(-0.02, 0.02)
        prices.append(p)
    # ends roughly where it churned; a single 2% hourly wiggle should NOT clear
    # the bar because sigma is large in this regime
    m = D(hourly(now, prices), now)
    assert m is None or m.pct > 6   # only an extreme break would fire here


def test_dead_market_small_real_move_fires():
    now = 1_000_000.0
    flat = [76000 + (20 if i % 2 else -20) for i in range(22)]  # sigma ~ tiny
    move = [76900, 77400, 77700]   # +2.2% is huge relative to this churn
    m = D(hourly(now, flat + move), now)
    assert m and m.direction == "up"


def test_needs_min_history():
    now = 1_000_000.0
    assert D(hourly(now, [78000, 79000, 80000, 81000]), now) is None  # < min_hours


def test_new_leg_gating():
    now = 1_000_000.0
    up = btcwatch.Move("up", 4.2, 77900, 81253, 81253, 180)
    assert btcwatch.is_new_leg(up, None, threshold_pct=1.2, reexplain_after_s=12 * H, now_ts=now)
    last = {"direction": "up", "reference_price": 81253, "ts": now - 3600}
    plateau = btcwatch.Move("up", 4.3, 77900, 81400, 81400, 180)
    assert not btcwatch.is_new_leg(plateau, last, threshold_pct=1.2, reexplain_after_s=12 * H, now_ts=now)
    leg2 = btcwatch.Move("up", 5.0, 77900, 82500, 82500, 200)     # +1.5% beyond ref
    assert btcwatch.is_new_leg(leg2, last, threshold_pct=1.2, reexplain_after_s=12 * H, now_ts=now)
    down = btcwatch.Move("down", 3.0, 78700, 81253, 78700, 90)
    assert btcwatch.is_new_leg(down, last, threshold_pct=1.2, reexplain_after_s=12 * H, now_ts=now)
    assert btcwatch.is_new_leg(plateau, {**last, "ts": now - 13 * H}, threshold_pct=1.2, reexplain_after_s=12 * H, now_ts=now)


def test_history_roundtrip_and_prune(tmp_path):
    s = Settings(state_dir=tmp_path)
    now = time.time()
    btcwatch.save_history(s, [(now - 40 * H, 70000.0), (now - 2 * H, 80000.0), (now, 81000.0)])
    got = btcwatch.load_history(s)
    assert len(got) == 3 and got[-1][1] == 81000.0


def test_spoken_age():
    now = 1_000_000.0
    assert btcwatch.spoken({"text": "Bitcoin rose on ETF inflows.", "ts": now - 30 * 60}, now) == \
        "Bitcoin rose on ETF inflows. That was just now."
    assert btcwatch.spoken({"text": "X.", "ts": now - 3 * H}, now) == "X. That was about 3 hours ago."


def test_clean_answer_strips_sources_and_urls():
    raw = ("Bitcoin rose on a short squeeze after the Fed. It was a broad rally.\n\n"
           "Sources: [CoinDesk](https://coindesk.com/x), [Yahoo](https://finance.yahoo.com/y)")
    out = btcwatch._clean_answer(raw)
    assert out == "Bitcoin rose on a short squeeze after the Fed. It was a broad rally."
    assert "http" not in out and "Sources" not in out
    assert btcwatch._clean_answer("See [here](http://x) for **details**.") == "See here for details."


def test_notify_defers_overnight_and_flushes_in_morning(tmp_path, monkeypatch):
    posted = []
    from switchboard import btcwatch as bw
    monkeypatch.setattr(bw, "_post_ntfy", lambda settings, title, text: posted.append((title, text)) or True)
    s = Settings(state_dir=tmp_path, quiet_start_h=22, quiet_end_h=7)
    m = bw.Move("up", 3.7, 77993, 80896, 80896, 180)
    # 3 a.m. -> deferred, nothing posted
    monkeypatch.setattr(bw, "_is_quiet", lambda settings, now_ts=None: True)
    bw._notify(s, m, "Bitcoin rose on ETF inflows.")
    assert posted == [] and (tmp_path / "btc-pending-ntfy.json").exists()
    assert bw.flush_pending(s) == 0            # still quiet -> held
    # 8 a.m. -> flush releases it once, prefixed Overnight
    monkeypatch.setattr(bw, "_is_quiet", lambda settings, now_ts=None: False)
    assert bw.flush_pending(s) == 1
    assert posted[0][0].startswith("Overnight: Bitcoin up 3.7%")
    assert not (tmp_path / "btc-pending-ntfy.json").exists()
    assert bw.flush_pending(s) == 0            # nothing left


def test_notify_posts_immediately_in_daytime(tmp_path, monkeypatch):
    posted = []
    from switchboard import btcwatch as bw
    monkeypatch.setattr(bw, "_post_ntfy", lambda settings, title, text: posted.append(title) or True)
    monkeypatch.setattr(bw, "_is_quiet", lambda settings, now_ts=None: False)
    bw._notify(Settings(state_dir=tmp_path), bw.Move("down", 4.0, 78000, 81000, 78000, 120), "x")
    assert len(posted) == 1 and posted[0].startswith("Bitcoin down 4.0%")


# ---------------------------------------------------------------- feed


def _parse_atom_like_newsdesk(xml: str):
    """Extract entries exactly as newsdesk/feeds.parse_feed does, so this test
    fails if the two ever drift. Keeps the switchboard package standalone."""
    import xml.etree.ElementTree as ET

    ATOM = "http://www.w3.org/2005/Atom"
    root = ET.fromstring(xml)
    assert root.tag == f"{{{ATOM}}}feed"          # newsdesk keys on exactly this
    out = []
    for e in root.findall(f"{{{ATOM}}}entry"):
        link = ""
        for ln in e.findall(f"{{{ATOM}}}link"):
            if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                link = ln.get("href", "")
                break
        out.append({
            "guid": (e.findtext(f"{{{ATOM}}}id") or link),
            "url": link,
            "title": e.findtext(f"{{{ATOM}}}title"),
            "content": e.findtext(f"{{{ATOM}}}content"),
            "published": e.findtext(f"{{{ATOM}}}published"),
        })
    return out


def test_render_atom_roundtrips_through_newsdesk_parser():
    base = "https://digest.rosemaryacres.com/btc-moves.xml"
    recs = [
        {"ts": 1_700_000_000, "direction": "up", "pct": 3.7, "now": 80896,
         "lo": 77993, "hi": 80896, "text": "Bitcoin rose after a spot-ETF inflow surge."},
        {"ts": 1_700_100_000, "direction": "down", "pct": 4.1, "now": 76000,
         "lo": 76000, "hi": 79200, "text": "A hotter-than-expected CPI print hit risk assets."},
    ]
    entries = _parse_atom_like_newsdesk(btcwatch.render_atom(recs, base))
    assert len(entries) == 2
    # newest first
    assert entries[0]["content"] == "A hotter-than-expected CPI print hit risk assets."
    assert entries[1]["content"] == "Bitcoin rose after a spot-ETF inflow surge."
    # every field newsdesk needs is present and usable
    for e in entries:
        assert e["title"] and e["url"] and e["content"] and e["published"]
        assert e["title"].startswith("Bitcoin ")
        assert e["published"].endswith("Z")
    # ids/links are stable and unique so INSERT OR IGNORE de-dupes on re-poll
    guids = [e["guid"] for e in entries]
    assert len(set(guids)) == 2 and all(g.startswith(base + "#move-") for g in guids)
    assert entries[1]["title"] == "Bitcoin up 3.7% to $80,896"


def test_write_feed_appends_rolls_and_renders(tmp_path):
    feed = tmp_path / "digest" / "btc-moves.xml"
    s = Settings(state_dir=tmp_path, btc_feed_path=feed,
                 btc_feed_base_url="https://x/btc-moves.xml", btc_feed_keep=3)
    for i in range(5):
        btcwatch.write_feed(s, btcwatch.Move("up", float(i), 70000, 70000 + i, 70000 + i, 60),
                            f"move {i}", 1_700_000_000 + i)
    assert feed.exists()                         # digest dir created as needed
    entries = _parse_atom_like_newsdesk(feed.read_text())
    assert len(entries) == 3                      # rolled to btc_feed_keep
    assert entries[0]["content"] == "move 4"      # newest first
    assert entries[-1]["content"] == "move 2"     # oldest kept


def test_write_feed_disabled_when_path_empty(tmp_path):
    from pathlib import Path
    s = Settings(state_dir=tmp_path, btc_feed_path=Path(""))
    # must not raise and must not create the moves record
    btcwatch.write_feed(s, btcwatch.Move("up", 3.0, 70000, 72000, 72000, 60), "x", 1_700_000_000)
    assert not (tmp_path / "btc-moves.json").exists()
