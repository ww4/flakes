import json
import os
import time
from pathlib import Path

from switchboard import intents, standing
from switchboard.config import Settings


def q(**kw) -> standing.StandingQuestion:
    base = dict(name="t", patterns=[r"\bbackup"], ask="did it run?")
    base.update(kw)
    return standing.StandingQuestion(**base)


def test_route_matches_standing_after_fixed_rules() -> None:
    assert intents.route("did the backup run last night") == "standing:backups"
    assert intents.route("what's on my schedule today") == "standing:schedule"
    assert intents.route("what's the weather tomorrow") == "standing:forecast"
    assert intents.route("what did ryan hall say") == "standing:ryan-hall"
    # fixed rules still win
    assert intents.route("what's the status") == "status"
    assert intents.route("what time is it") == "time"


def test_fingerprint_tracks_watched_files_and_date(tmp_path: Path) -> None:
    f = tmp_path / "a.ics"
    f.write_text("x")
    sq = q(watch=[str(tmp_path / "*.ics")], daily=True)
    fp1 = standing.fingerprint(sq, now=1_800_000_000)
    assert standing.fingerprint(sq, now=1_800_000_000) == fp1          # stable
    os.utime(f, (1, 1))
    assert standing.fingerprint(sq, now=1_800_000_000) != fp1          # mtime moved
    fp2 = standing.fingerprint(sq, now=1_800_000_000)
    assert standing.fingerprint(sq, now=1_800_000_000 + 86400) != fp2  # next day


def test_needs_refresh_reasons(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path)
    sq = q(max_age_s=100)
    assert standing.needs_refresh(sq, None) == "no stored answer"
    st = standing.store(s, "t", "yes", standing.fingerprint(sq))
    assert standing.needs_refresh(sq, st) is None
    old = standing.Stored(text="yes", ts=time.time() - 1000, fingerprint=st.fingerprint)
    assert standing.needs_refresh(sq, old) == "max age"
    moved = standing.Stored(text="yes", ts=time.time(), fingerprint="different")
    assert standing.needs_refresh(sq, moved) == "sources changed"


def test_spoken_prefixes_age_only_when_old() -> None:
    fresh = standing.Stored(text="Yes.", ts=time.time() - 60, fingerprint="")
    assert standing.spoken(fresh) == "Yes."
    old = standing.Stored(text="Yes.", ts=time.time() - 3 * 3600, fingerprint="")
    assert standing.spoken(old) == "As of about 3 hours ago. Yes."


def test_slowlog_groups_rephrasings(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path)
    standing.log_slow(s, "Did the Immich import finish?", 9.0)
    standing.log_slow(s, "did the immich import finish", 7.0)
    standing.log_slow(s, "Is the Immich import finished?", 8.0)
    standing.log_slow(s, "how many photos are in immich", 5.0)
    rows = standing.slow_report(s)
    assert rows[0][1] == 2 and rows[0][0] == "finish immich import"   # sorted, stopwords dropped
    cands = standing.candidates(s, intents.standing_questions(), min_hits=2)
    assert len(cands) == 1 and cands[0][1] == 2
    # a repeated question a standing question already covers is not a candidate
    for _ in range(3):
        standing.log_slow(s, "did the backup run", 4.0)
    assert all("backup" not in c[0] for c in standing.candidates(s, intents.standing_questions(), min_hits=2))


def test_store_is_atomic_and_round_trips(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path)
    standing.store(s, "x", "hello", "fp")
    assert not list(tmp_path.glob("answers/*.part"))
    got = standing.load(s, "x")
    assert got is not None and got.text == "hello" and got.fingerprint == "fp"
    assert standing.load(s, "missing") is None
