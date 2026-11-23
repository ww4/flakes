"""owen-usage: daily household electric usage from Owen Electric's member portal.

Signs in to billing.owenelectric.com once a day with Chris's portal login,
calls the same JSON endpoint the Usage History chart uses, and keeps the whole
daily history in a state file. Serves it on 127.0.0.1 only, for Grafana's
Infinity datasource (JSON) and for Prometheus (health metrics).

Discovered 2026-11-23 by watching the portal's own traffic (see the
owen-electric-usage-api memory):

  POST /onlineportal/DesktopModules/MeterUsage/API/MeterData.aspx/GetDailyUsageData
  body {'keymbr','MemberSep','StartDate','EndDate','IsEnergy','IsPPM','IsCostEnable'}

READ ONLY BY CONSTRUCTION: the only API method this program can call is in
ALLOWED_METHODS. The portal's scripts also expose UpdateUsageNotes and
DeleteUsageNotes, which write; they are unreachable from here.

Data notes:
  * Real daily data starts 2023-11-01; the portal zero-pads every day before
    that, so days before FIRST_DAY are never stored.
  * Zeros AFTER FIRST_DAY are real. 2025-04-20 onward had 17 days of zero
    because the house flooded and was evacuated. They are stored as 0, never
    treated as gaps.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

BASE = "https://billing.owenelectric.com/onlineportal"
LOGIN_URL = f"{BASE}/Customer-Login"
USAGE_PAGE = f"{BASE}/My-Account/Usage-History"
API = f"{BASE}/DesktopModules/MeterUsage/API/MeterData.aspx"
ALLOWED_METHODS = frozenset({"GetDailyUsageData"})

STATE_DIR = os.environ.get("STATE_DIRECTORY", "/var/lib/owen-usage")
STATE_FILE = os.path.join(STATE_DIR, "daily.json")
BILLS_FILE = os.path.join(STATE_DIR, "bills.json")
PORT = int(os.environ.get("OWEN_USAGE_PORT", "9208"))
FIRST_DAY = date.fromisoformat(os.environ.get("OWEN_USAGE_FIRST_DAY", "2023-11-01"))
REFRESH_DAYS = int(os.environ.get("OWEN_USAGE_REFRESH_DAYS", "30"))
RUN_AT = os.environ.get("OWEN_USAGE_RUN_AT", "07:30")          # local time, daily
RETRY_SECONDS = int(os.environ.get("OWEN_USAGE_RETRY_SECONDS", "3600"))
PACE_SECONDS = 3.0                                             # between portal calls
UA = "Mozilla/5.0 (X11; Linux x86_64) owen-usage/1 (household usage history)"


class PortalError(RuntimeError):
    """Login or data fetch failed. Messages never contain credentials or account ids."""


# ── credentials ─────────────────────────────────────────────────────────────

def load_credentials() -> tuple[str, str]:
    """Read KEY=VALUE lines literally: no quoting, no $ expansion, value is
    everything after the first '='. The file comes from sops via systemd
    LoadCredential, so it is never sourced by a shell."""
    cdir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not cdir:
        raise PortalError("CREDENTIALS_DIRECTORY not set (systemd LoadCredential missing)")
    vals: dict[str, str] = {}
    with open(os.path.join(cdir, "login")) as f:
        for line in f:
            line = line.rstrip("\r\n")
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v
    user, pw = vals.get("OWEN_ELECTRIC_USERNAME"), vals.get("OWEN_ELECTRIC_PASSWORD")
    if not user or not pw:
        raise PortalError("login file lacks OWEN_ELECTRIC_USERNAME / OWEN_ELECTRIC_PASSWORD")
    return user, pw


# ── portal session ──────────────────────────────────────────────────────────

class _Inputs(HTMLParser):
    """Collect <input name=… value=…> pairs from a page."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.ids: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        a = {k: (v or "") for k, v in attrs}
        if a.get("name"):
            self.fields.setdefault(a["name"], a.get("value", ""))
        if a.get("id"):
            self.ids[a["id"]] = a.get("value", "")


def _inputs(html: str) -> _Inputs:
    p = _Inputs()
    p.feed(html)
    return p


class Portal:
    def __init__(self) -> None:
        self.http = httpx.Client(timeout=60.0, follow_redirects=True, headers={"User-Agent": UA})
        self.keymbr = ""
        self.mbrsep = ""

    def close(self) -> None:
        self.http.close()

    def login(self, user: str, pw: str) -> None:
        r = self.http.get(LOGIN_URL)
        r.raise_for_status()
        form = _inputs(r.text).fields
        hidden = {k: v for k, v in form.items()
                  if k.startswith("__") or k in ("ScrollTop",)}
        if "__VIEWSTATE" not in hidden:
            raise PortalError("login page has no __VIEWSTATE; the portal layout changed")
        btn = next((k for k in form if k.endswith("$CustomerLogin$btnLogin")), None)
        uname = next((k for k in form if k.endswith("$CustomerLogin$txtUsername")), None)
        pword = next((k for k in form if k.endswith("$CustomerLogin$txtPassword")), None)
        if not (btn and uname and pword):
            raise PortalError("login form fields not found; the portal layout changed")
        data = {**hidden, "__EVENTTARGET": "", "__EVENTARGUMENT": "",
                uname: user, pword: pw, btn: "Sign In"}
        time.sleep(PACE_SECONDS)
        r = self.http.post(LOGIN_URL, data=data)
        r.raise_for_status()
        m = re.search(r'id="dnn_ctr\d+_CustomerLogin_errorMessage">([^<]+)<', r.text)
        if m and m.group(1).strip():
            raise PortalError(f"portal refused the login: {m.group(1).strip()[:120]}")
        if "openMFAVerify();" in r.text:
            raise PortalError("portal asked for a verification code (MFA); unattended login is not possible")
        time.sleep(PACE_SECONDS)
        r = self.http.get(USAGE_PAGE)
        r.raise_for_status()
        ids = _inputs(r.text).ids
        self.keymbr = next((v for k, v in ids.items() if k.endswith("_MeterUsage_hdnkeymbr")), "")
        self.mbrsep = next((v for k, v in ids.items() if k.endswith("_MeterUsage_hdnmbrsep")), "")
        if not (self.keymbr and self.mbrsep):
            raise PortalError("signed in, but Usage History carried no account ids (login did not stick?)")

    def _call(self, method: str, body: dict[str, str]) -> Any:
        if method not in ALLOWED_METHODS:
            raise PortalError(f"refusing to call {method}: not in ALLOWED_METHODS")
        # The portal's own JS builds this body with single quotes; mirror it.
        payload = json.dumps(body).replace('"', "'")
        r = self.http.post(f"{API}/{method}", content=payload, headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": USAGE_PAGE,
        })
        if r.status_code != 200:
            raise PortalError(f"{method} returned HTTP {r.status_code}")
        d = r.json().get("d")
        if not isinstance(d, dict):
            raise PortalError(f"{method} returned no data object")
        err = d.get("errorObject") or {}
        if err.get("errorid"):
            raise PortalError(f"{method} error: {err.get('errortype') or err.get('errorid')}")
        return d

    def daily(self, start: date, end: date) -> list[dict[str, Any]]:
        d = self._call("GetDailyUsageData", {
            "keymbr": self.keymbr, "MemberSep": self.mbrsep,
            "StartDate": start.strftime("%m/%d/%Y"), "EndDate": end.strftime("%m/%d/%Y"),
            "IsEnergy": "false", "IsPPM": "false", "IsCostEnable": "3",
        })
        return [r for r in (parse_day(i) for i in d.get("Items") or []) if r]


# ── data ────────────────────────────────────────────────────────────────────

def _num(v: Any) -> float | None:
    if v in (None, "", "NaN"):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def parse_day(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        day = datetime.strptime(item["DAILYTIMESTAMP"], "%m/%d/%Y").date()
    except (KeyError, ValueError):
        return None
    if day < FIRST_DAY:
        return None
    kwh = _num(item.get("TOTALENERGY"))
    blank = kwh is None
    if blank:
        # An empty day is stored as 0, flagged. On this meter that has meant
        # real zero usage (the 2025 flood evacuation came back empty, not 0).
        # The newest days can be empty only because the portal lags, so those
        # are skipped; the 30-day refresh fills them once they post.
        if day >= date.today() - timedelta(days=2):
            return None
        kwh = 0.0
    return {
        "date": day.isoformat(),
        # Local midnight, in ms: Grafana plots the bar on the day it belongs to.
        "time": int(datetime(day.year, day.month, day.day).timestamp() * 1000),
        "kwh": kwh,
        "source": "portal",
        "blank": blank,
        "end_reading": _num(item.get("ENDREADING")),
        "temp_min_f": _num(item.get("MIN_TEMP")),
        "temp_max_f": _num(item.get("MAX_TEMP")),
        "temp_avg_f": _num(item.get("AVG_TEMP")),
        "hdd": _num(item.get("H_DEGREE_DAY")),
        "cdd": _num(item.get("C_DEGREE_DAY")),
    }


def load_state() -> dict[str, dict[str, Any]]:
    try:
        with open(STATE_FILE) as f:
            return {r["date"]: r for r in json.load(f)}
    except FileNotFoundError:
        return {}


def bill_days() -> dict[str, dict[str, Any]]:
    """History from scanned paper bills, older than the portal keeps.

    BILLS_FILE is a hand-maintained JSON list, one entry per bill:
      [{"start": "2021-03-04", "end": "2021-04-02", "kwh": 1310}, ...]
    start and end are the service dates printed on the bill, both inclusive.
    Each bill's kWh is spread evenly over its days, so a daily chart shows
    flat steps here (a visible change of granularity) while weekly, monthly
    and yearly sums stay correct. Portal days always win over bill days.
    Lives in the state directory, never in the flake: it is household data,
    and the flake repo is mirrored to GitHub.
    """
    try:
        with open(BILLS_FILE) as f:
            bills = json.load(f)
    except FileNotFoundError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for b in bills:
        start, end = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
        n = (end - start).days + 1
        if n <= 0 or n > 62:
            raise ValueError(f"bill {b['start']}..{b['end']}: implausible period of {n} days")
        per = float(b["kwh"]) / n
        for i in range(n):
            d = start + timedelta(days=i)
            if d >= FIRST_DAY:
                continue
            out[d.isoformat()] = {
                "date": d.isoformat(),
                "time": int(datetime(d.year, d.month, d.day).timestamp() * 1000),
                "kwh": round(per, 3), "source": "bill",
                "end_reading": None, "temp_min_f": None, "temp_max_f": None,
                "temp_avg_f": None, "hdd": None, "cdd": None,
            }
    return out


def merged(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        allrows = {**bill_days(), **rows}
    except (ValueError, KeyError, TypeError) as e:
        print(f"owen-usage: ignoring {BILLS_FILE}: {e}", file=sys.stderr, flush=True)
        allrows = dict(rows)
    return [allrows[k] for k in sorted(allrows)]


def save_state(rows: dict[str, dict[str, Any]]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump([rows[k] for k in sorted(rows)], f, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


def monthly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["date"][:7]
        m = out.setdefault(key, {"month": key, "kwh": 0.0, "days": 0,
                                 "time": int(datetime(int(key[:4]), int(key[5:]), 1).timestamp() * 1000)})
        m["kwh"] += r["kwh"]
        m["days"] += 1
    return [out[k] for k in sorted(out)]


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    by = {r["date"]: r["kwh"] for r in rows}
    last = date.fromisoformat(rows[-1]["date"])

    def window(end: date, days: int) -> float | None:
        vals = [by.get((end - timedelta(days=i)).isoformat()) for i in range(days)]
        return None if any(v is None for v in vals) else sum(vals)  # type: ignore[arg-type]

    try:
        year_ago = last.replace(year=last.year - 1)
    except ValueError:                      # Feb 29
        year_ago = last - timedelta(days=365)
    return {
        "latest_day": last.isoformat(),
        "latest_kwh": by[last.isoformat()],
        "last_7_kwh": window(last, 7),
        "last_30_kwh": window(last, 30),
        "same_30_last_year_kwh": window(year_ago, 30),
        "year_to_date_kwh": sum(v for d, v in by.items() if d.startswith(str(last.year))),
    }


# ── fetch loop ──────────────────────────────────────────────────────────────

class Status:
    lock = threading.Lock()
    ok = 0
    last_success = 0.0
    last_error = ""
    rows: list[dict[str, Any]] = []


def fetch_once() -> None:
    user, pw = load_credentials()
    rows = load_state()
    today = date.today()
    if rows:
        start = max(FIRST_DAY, today - timedelta(days=REFRESH_DAYS))
    else:
        start = FIRST_DAY                      # first run: the whole history
    portal = Portal()
    try:
        portal.login(user, pw)
        chunk_start = start
        got = 0
        while chunk_start < today:
            chunk_end = min(chunk_start + timedelta(days=364), today - timedelta(days=1))
            time.sleep(PACE_SECONDS)
            for r in portal.daily(chunk_start, chunk_end):
                rows[r["date"]] = r
                got += 1
            chunk_start = chunk_end + timedelta(days=1)
    finally:
        portal.close()
    if not rows:
        raise PortalError("portal returned no usage days")
    save_state(rows)
    ordered = merged(rows)
    with Status.lock:
        Status.rows = ordered
        Status.ok = 1
        Status.last_success = time.time()
        Status.last_error = ""
    print(f"owen-usage: fetched {got} day(s) from {start}; {len(ordered)} stored, "
          f"latest {ordered[-1]['date']} = {ordered[-1]['kwh']:g} kWh", flush=True)


def next_run(now: datetime) -> datetime:
    hh, mm = (int(x) for x in RUN_AT.split(":"))
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def loop() -> None:
    portal_rows = load_state()
    with Status.lock:
        Status.rows = merged(portal_rows)
    newest = max(portal_rows) if portal_rows else None
    stale = newest is None or date.fromisoformat(newest) < date.today() - timedelta(days=1)
    wait = 5.0 if stale else (next_run(datetime.now()) - datetime.now()).total_seconds()
    while True:
        time.sleep(max(wait, 1.0))
        try:
            fetch_once()
            wait = (next_run(datetime.now()) - datetime.now()).total_seconds()
        except Exception as e:                 # noqa: BLE001 - logged, retried, and exported as a metric
            msg = f"{type(e).__name__}: {e}"[:300]
            with Status.lock:
                Status.ok = 0
                Status.last_error = msg
            print(f"owen-usage: fetch failed, retrying in {RETRY_SECONDS}s: {msg}", file=sys.stderr, flush=True)
            wait = RETRY_SECONDS


# ── HTTP (127.0.0.1 only) ───────────────────────────────────────────────────

def metrics() -> str:
    with Status.lock:
        rows, ok, last, err = Status.rows, Status.ok, Status.last_success, Status.last_error
    lines = [
        "# HELP owen_usage_fetch_success 1 if the last portal fetch succeeded.",
        "# TYPE owen_usage_fetch_success gauge",
        f"owen_usage_fetch_success {ok}",
        "# HELP owen_usage_last_success_timestamp_seconds Time of the last successful fetch.",
        "# TYPE owen_usage_last_success_timestamp_seconds gauge",
        f"owen_usage_last_success_timestamp_seconds {last:.0f}",
        "# HELP owen_usage_days_stored Days of usage history held.",
        "# TYPE owen_usage_days_stored gauge",
        f"owen_usage_days_stored {len(rows)}",
        "# HELP owen_usage_last_error 1 while the last fetch failed, labelled with a short reason.",
        "# TYPE owen_usage_last_error gauge",
    ]
    if err:
        reason = re.sub(r'["\\\n]', " ", err.split(":")[0])[:60]
        lines.append(f'owen_usage_last_error{{reason="{reason}"}} 1')
    if rows:
        d = date.fromisoformat(rows[-1]["date"])
        lines += [
            "# HELP owen_usage_latest_day_timestamp_seconds Local midnight of the newest day held.",
            "# TYPE owen_usage_latest_day_timestamp_seconds gauge",
            f"owen_usage_latest_day_timestamp_seconds {datetime(d.year, d.month, d.day).timestamp():.0f}",
            "# HELP owen_usage_latest_day_kwh Household kWh on the newest day held.",
            "# TYPE owen_usage_latest_day_kwh gauge",
            f"owen_usage_latest_day_kwh {rows[-1]['kwh']:g}",
        ]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:                  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        with Status.lock:
            rows = Status.rows
        if path == "/daily.json":
            body, ctype = json.dumps(rows), "application/json"
        elif path == "/monthly.json":
            body, ctype = json.dumps(monthly(rows)), "application/json"
        elif path == "/summary.json":
            body, ctype = json.dumps([summary(rows)]), "application/json"
        elif path == "/metrics":
            body, ctype = metrics(), "text/plain; version=0.0.4"
        else:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        fetch_once()
        return
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
