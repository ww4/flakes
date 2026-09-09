"""wx command line — one subcommand per systemd unit.

  wx alerts    LAYER 1. Poll NWS for this point, notify what is new. Fast timer.
  wx ryan      LAYER 2 + 3. Poll the channel, transcribe, extract, render for
               the newsdesk, and decide whether any of it earns an interruption.
  wx morning   Flush anything quiet hours held, as one message. 07:05.
  wx spc       Refresh the SPC picture. Also a useful hand-run for debugging.
  wx status    Read-only summary. Safe to call from /health at any time.

Every subcommand is exit-0 on "nothing to do" and non-zero only when it could
not do its job — because a timer that reports failure for a quiet Tuesday is a
timer whose failures nobody reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db, extract, notify, render, rules, ryan, spc
from .nws import fetch as nws_fetch
from .nws import process as nws_process


def _ua() -> str:
    # NWS asks for a contactable identifier. No coordinates in it.
    return os.environ.get("WX_USER_AGENT", "(gromit-wx, chris.saenz@broadlinc.com)")


def _location() -> tuple[float, float]:
    path = os.environ.get("WX_LOCATION_FILE", "/run/secrets/wx-location")
    return db.load_location(path)


def _corpus_dir() -> Path:
    return Path(os.environ.get("WX_CORPUS_DIR", str(db.state_dir() / "corpus")))


def _prompt_path() -> Path:
    return Path(os.environ.get("WX_PROMPT", "/etc/wx/extract-prompt.md"))


def cmd_alerts(args) -> int:
    con = db.connect()
    try:
        lat, lon = _location()
    except (OSError, ValueError) as exc:
        # Deliberately does not print the path's CONTENTS on a parse failure.
        print(f"wx: cannot read location: {exc}", file=sys.stderr)
        return 1
    try:
        alerts = nws_fetch(lat, lon, user_agent=_ua())
    except Exception as exc:                      # noqa: BLE001 - network is broad
        print(f"wx: NWS fetch failed: {exc}", file=sys.stderr)
        return 1

    fresh = nws_process(con, alerts)
    print(f"wx: {len(alerts)} active, {len(fresh)} new to report")
    for item in fresh:
        state = notify.deliver(
            con, layer=1, key=item["key"], title=item["title"],
            body=item["body"], alert_class=item["class"],
            notifier=args.notifier)
        print(f"wx: [{item['class']}] {item['event']} -> {state}")
    return 0


def cmd_spc(args) -> int:
    con = db.connect()
    try:
        lat, lon = _location()
    except (OSError, ValueError) as exc:
        print(f"wx: cannot read location: {exc}", file=sys.stderr)
        return 1
    labels = spc.refresh(con, lat, lon, user_agent=_ua())
    if not labels:
        print("wx: SPC unreachable — leaving the previous picture in place",
              file=sys.stderr)
        return 1
    for day in sorted(labels):
        print(f"wx: SPC day{day}: {labels[day] or 'no risk area over the point'}")
    return 0


def _spc_note(drawn: set[int], known: set[int]) -> str:
    if not known:
        return "SPC outlook unavailable at the time of writing"
    if drawn:
        return ("SPC has a severe risk drawn over the house on day "
                + ", ".join(str(d) for d in sorted(drawn)))
    return "SPC has nothing drawn over the house through day 3"


def cmd_ryan(args) -> int:
    con = db.connect()

    try:
        videos = ryan.list_recent(ytdlp=args.ytdlp)
    except Exception as exc:                      # noqa: BLE001
        print(f"wx: channel listing failed: {exc}", file=sys.stderr)
        return 1
    added = ryan.sync_listing(con, videos)
    print(f"wx: {len(videos)} long-form uploads listed, {added} new")

    # SPC first: the correlation is only meaningful against a current picture,
    # and it is cheap.
    try:
        lat, lon = _location()
        spc.refresh(con, lat, lon, user_agent=_ua())
    except Exception as exc:                      # noqa: BLE001
        print(f"wx: SPC refresh failed, continuing on the cached picture: {exc}",
              file=sys.stderr)
    drawn, known = spc.drawn_days(con), spc.known_days(con)

    prompt_base = _prompt_path().read_text(encoding="utf-8")
    titles = {v["id"]: v for v in videos}

    for row in ryan.pending_videos(con):
        vid = row["id"]
        try:
            transcript = ryan.fetch_transcript(vid, ytdlp=args.ytdlp)
        except Exception as exc:                  # noqa: BLE001
            state = ryan.record_failure(con, vid, str(exc))
            print(f"wx: {vid} transcript {state}: {str(exc)[:120]}", file=sys.stderr)
            continue
        ryan.store_transcript(con, vid, transcript)

        video = {"id": vid, "title": row["title"], "duration": row["duration"],
                 "published": titles.get(vid, {}).get("published")}
        prompt = extract.build_prompt(prompt_base, video, transcript,
                                      extract.previous_extractions(con))
        try:
            raw = extract.run_claude(prompt, claude=args.claude,
                                     timeout=args.extract_timeout)
            fields = extract.normalise(extract.parse_response(raw))
        except Exception as exc:                  # noqa: BLE001
            # An extraction failure is not fatal and must not lose the
            # transcript — the row keeps state='have' and the next run retries.
            print(f"wx: extraction failed for {vid}: {str(exc)[:200]}", file=sys.stderr)
            continue
        extract.store(con, vid, fields, raw)

        note = _spc_note(drawn, known)
        body = render.render_post(video=video, fields=fields,
                                  transcript=transcript, spc_note=note)
        path = render.write_post(_corpus_dir(), video, body)
        words = render.word_count(body)
        if words < render.NEWSDESK_MIN_WORDS:
            # Loud, because the newsdesk would drop it in silence.
            print(f"wx: WARNING {path.name} is {words} words — under the newsdesk"
                  f" corpus floor of {render.NEWSDESK_MIN_WORDS}; it will not be"
                  " ingested", file=sys.stderr)
        print(f"wx: wrote {path.name} ({words} words)")

        decision = rules.decide(con, fields, drawn=drawn, known=known)
        print(f"wx: layer 3 -> {'PUSH' if decision.push else 'quiet'}: {decision.reason}")
        if decision.push:
            title = f"Ryan Hall: {', '.join(fields['hazards'][:2])} — {fields['window_start'] or 'soon'}"
            state = notify.deliver(
                con, layer=3, key=f"ryan:{fields.get('system_id') or 'unknown'}",
                title=title,
                body=f"{fields['summary']}\n\n{decision.reason}\n"
                     f"https://www.youtube.com/watch?v={vid}",
                alert_class="warning", notifier=args.notifier, tags="cloud_tornado")
            print(f"wx: trend push -> {state}")
    return 0


def cmd_morning(args) -> int:
    con = db.connect()
    n = notify.flush_pending(con, notifier=args.notifier)
    print(f"wx: flushed {n} held item(s)")
    return 0


def cmd_status(args) -> int:
    con = db.connect()
    out = {
        "active_alerts_seen": con.execute(
            "SELECT COUNT(*) c FROM nws_alert").fetchone()["c"],
        "notified": con.execute(
            "SELECT COUNT(*) c FROM nws_alert WHERE notified_class IS NOT NULL"
        ).fetchone()["c"],
        "videos": con.execute("SELECT COUNT(*) c FROM video").fetchone()["c"],
        "transcripts": con.execute(
            "SELECT COUNT(*) c FROM video WHERE state='have'").fetchone()["c"],
        "deferred": con.execute(
            "SELECT COUNT(*) c FROM video WHERE state='deferred'").fetchone()["c"],
        "extractions": con.execute("SELECT COUNT(*) c FROM extraction").fetchone()["c"],
        "pending_notifications": con.execute(
            "SELECT COUNT(*) c FROM pending").fetchone()["c"],
        "spc": {str(r["day"]): r["label"] for r in
                con.execute("SELECT day, label FROM spc_state").fetchall()},
        "last_push": (con.execute(
            "SELECT sent_at FROM push ORDER BY sent_at DESC LIMIT 1").fetchone() or
            {"sent_at": None})["sent_at"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(out, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wx", description=__doc__)
    ap.add_argument("--notifier", default="gromit-notify")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("alerts", help="LAYER 1 — poll NWS and notify").set_defaults(fn=cmd_alerts)
    sub.add_parser("spc", help="refresh the SPC outlook picture").set_defaults(fn=cmd_spc)
    sub.add_parser("morning", help="flush what quiet hours held").set_defaults(fn=cmd_morning)
    sub.add_parser("status", help="read-only summary").set_defaults(fn=cmd_status)

    p = sub.add_parser("ryan", help="LAYER 2+3 — transcripts, extraction, trends")
    p.add_argument("--ytdlp", default="yt-dlp")
    p.add_argument("--claude", default="claude")
    p.add_argument("--extract-timeout", type=int, default=600)
    p.set_defaults(fn=cmd_ryan)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
