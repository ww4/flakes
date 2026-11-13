"""Build-time gate for rebooter-receiver.

Run as: python3 rebooter-receiver-test.py <path-to-built-script>

The script is built by writers.writePython3Bin and so has NO .py extension --
SourceFileLoader is what makes it importable. Importing is safe because the
script guards its entry point with __name__ == "__main__".

What is actually worth asserting here: this service exists to capture a payload
nobody has seen, so every test below is about NOT LOSING AN EVENT. A crash on a
malformed body, or a body silently dropped because it failed to parse, would
destroy the single observation the whole chain was built to make -- and we
would not get another until the link next fails.
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile

path = sys.argv[1]
loader = importlib.machinery.SourceFileLoader("rr", path)
spec = importlib.util.spec_from_loader("rr", loader)
rr = importlib.util.module_from_spec(spec)
loader.exec_module(rr)

failures = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        failures.append(name)


hdrs = {"X-Real-IP": "24.106.142.194", "Content-Type": "application/json"}

# 1. A well-formed body parses AND keeps the raw bytes.
rec = rr.build_record(1700000000.5, "POST", "/notify", "127.0.0.1", hdrs,
                      b'{"event":"reboot","reason":"ping_fail"}')
check("valid json parsed", rec.get("json", {}).get("event") == "reboot", rec)
check("raw kept alongside json", '"event"' in rec["raw"], rec["raw"])
check("no parse_error on valid body", "parse_error" not in rec, rec)
# ⚠️ This assertion used to read `X-Real-IP becomes client` and PASS, which
# encoded the bug: gromit's nginx overwrites X-Real-IP with lock3's address, so
# trusting it names the proxy, never the device. X-Real-IP alone is now ignored.
check("X-Real-IP alone does NOT name the client", rec["client"] == "127.0.0.1", rec["client"])
check("body_bytes counted", rec["body_bytes"] == 39, rec["body_bytes"])

# 2. A malformed body must still be RECORDED, not dropped.
rec = rr.build_record(1700000000.0, "POST", "/notify", "127.0.0.1", {},
                      b"this is not json")
check("malformed body still recorded", rec["raw"] == "this is not json", rec)
check("malformed body flags parse_error", "parse_error" in rec, rec)
check("no client header falls back to peer", rec["client"] == "127.0.0.1", rec)

# 2b. The client field must name the DEVICE, not the last proxy. Two hops sit in
#     front of this service and gromit's nginx overwrites X-Real-IP with lock3's
#     address, so trusting it would silently attribute every event to the VPS.
two_hops = {"X-Real-IP": "100.125.31.125",
            "X-Forwarded-For": "24.106.142.194, 100.125.31.125"}
rec = rr.build_record(1700000000.0, "POST", "/notify", "127.0.0.1", two_hops, b"{}")
check("client is the ORIGINAL client, not the last proxy",
      rec["client"] == "24.106.142.194", rec["client"])
rec = rr.build_record(1700000000.0, "POST", "/notify", "9.9.9.9", {}, b"{}")
check("no XFF falls back to the peer", rec["client"] == "9.9.9.9", rec["client"])
rec = rr.build_record(1700000000.0, "POST", "/notify", "9.9.9.9",
                      {"X-Forwarded-For": "  "}, b"{}")
check("blank XFF falls back, does not yield empty", rec["client"] == "9.9.9.9", rec["client"])

# 3. Non-UTF-8 bytes must not raise -- this is the one that would lose the event.
rec = rr.build_record(1700000000.0, "POST", "/notify", "127.0.0.1", {},
                      b"\xff\xfe binary \x00")
check("non-utf8 body survives", "raw" in rec and rec["body_bytes"] == 11, rec)
check("non-utf8 body still flags parse_error", "parse_error" in rec, rec)

# 4. Truncation is flagged rather than silent.
rec = rr.build_record(1700000000.0, "POST", "/notify", "127.0.0.1", {},
                      b"x" * 10, truncated=True)
check("truncation flagged", rec.get("truncated") is True, rec)
check("truncation records the cap", rec.get("truncated_at") == rr.MAX_BODY, rec)

# 5. Every record must survive json.dumps -- it is written as NDJSON.
rec = rr.build_record(1700000000.0, "POST", "/notify", "1.2.3.4", hdrs, b"{}")
try:
    json.dumps(rec)
    check("record is json-serialisable", True)
except Exception as exc:
    check("record is json-serialisable", False, exc)

# 6. append_event APPENDS. Overwriting would discard earlier events, which for
#    a once-per-outage notification could mean losing months of history.
with tempfile.TemporaryDirectory() as tmp:
    state = os.path.join(tmp, "nested", "state")
    rr.append_event(state, {"n": 1})
    written = rr.append_event(state, {"n": 2})
    lines = open(written, encoding="utf-8").read().strip().split("\n")
    check("creates its state dir", os.path.isdir(state))
    check("appends, does not overwrite", len(lines) == 2, lines)
    check("each line is one json object",
          [json.loads(ln)["n"] for ln in lines] == [1, 2], lines)

if failures:
    print("\n%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("\nall rebooter-receiver tests passed")
