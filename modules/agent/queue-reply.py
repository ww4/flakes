"""queue-reply — publish a queue-item status to ntfy for agent-vm to consume.

Contract v1, agreed with agent-vm/herdr-management 2026-10-12. The field names
and regexes here are FIXED by that contract: if one has to change, it is
recorded in `Notes/Queue replies - gromit side` rather than diverged silently.

⚠️ WHY THERE IS NO FREE-TEXT FIELD. agent-vm types a fixed template built from
these values into a LIVE Claude session. Any free text we emit would therefore
be a prompt-injection channel straight into another agent's input. Every field
below is either a fixed vocabulary word or a value matched against a strict
regex, and human-readable content goes in the note instead.

⚠️ WHY THE CALLER IS A SCRIPT, NOT THE MODEL. The consumer parses the queue file
and calls this; the model that works the item never publishes. A model-authored
publish would put attacker-influenced text (a queue item is untrusted) one step
from agent-vm's session.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

STATUSES = ("accepted", "answered", "declined", "failed")

# Fixed by contract v1.
LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REQ_RE = re.compile(r"^av-([a-z][a-z0-9_-]{0,31})-([0-9]{14})$")
NOTE_RE = re.compile(r"^[A-Za-z0-9 _./()-]{1,200}\.md$")

# read_note's own allowlist, so "readable by read_note" is CHECKED, not assumed.
# Kept in step with HOMELAB_MCP_READABLE_SOURCES / UNREADABLE_PATHS.
READABLE_PREFIXES = ("Inbox/", "Projects/", "Areas/", "Notes/")
UNREADABLE_PREFIXES = ("Inbox/Log",)

URL = os.environ.get(
    "QUEUE_REPLY_URL", "https://ntfy.rosemaryacres.com/agent-queue-replies")
TOKEN_FILE = os.environ.get(
    "QUEUE_REPLY_TOKEN_FILE", "/var/lib/queue-reply/publisher-token.txt")
STATE = os.environ.get("QUEUE_REPLY_STATE", "/var/lib/queue-reply/published.json")
SPACE = os.environ.get("QUEUE_REPLY_SPACE", "/var/lib/silverbullet")


def die(msg: str) -> "None":
    print(f"queue-reply: {msg}", file=sys.stderr)
    raise SystemExit(2)


def validate(status: str, req: str, reply_to: str, note: str) -> str:
    if status not in STATUSES:
        die(f"status must be one of {', '.join(STATUSES)}")
    if not reply_to.startswith("agent-vm/"):
        die("reply_to must be 'agent-vm/<label>'")
    label = reply_to.split("/", 1)[1]
    if not LABEL_RE.match(label):
        die(f"label {label!r} fails the contract regex")
    m = REQ_RE.match(req)
    if not m:
        die(f"req {req!r} fails the contract regex (av-<label>-<YYYYMMDDHHMMSS>)")
    if m.group(1) != label:
        # Contract: "req must start with av-<label>-, where <label> is the one
        # in reply_to". Mismatched, this would route an answer to a pane that
        # did not ask for it.
        die(f"req label {m.group(1)!r} does not match reply_to label {label!r}")

    if status == "accepted":
        if note:
            die("note must be empty for 'accepted'")
        return ""
    if not note:
        die(f"note is required for status {status!r}")
    if ".." in note:
        die("note must not contain '..'")
    if not NOTE_RE.match(note):
        die(f"note {note!r} fails the contract regex")
    if not note.startswith(READABLE_PREFIXES):
        die(f"note must start with one of {READABLE_PREFIXES} "
            f"or read_note cannot read it")
    if note.startswith(UNREADABLE_PREFIXES):
        die("note is under an UNREADABLE_PATHS prefix; read_note would refuse it")
    # The contract says the note must be READABLE. Check the file, so we never
    # tell agent-vm to read something that is not there.
    path = os.path.join(SPACE, note)
    if not os.path.isfile(path):
        die(f"note does not exist: {note}")
    if not os.access(path, os.R_OK):
        die(f"note is not readable: {note}")
    return note


def load_state() -> dict:
    try:
        with open(STATE) as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def token() -> str:
    try:
        with open(TOKEN_FILE) as fh:
            return fh.read().strip()
    except OSError as exc:
        die(f"cannot read the publisher token at {TOKEN_FILE}: {exc}")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(prog="queue-reply")
    ap.add_argument("status", choices=STATUSES)
    ap.add_argument("--req", required=True)
    ap.add_argument("--reply-to", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    note = validate(args.status, args.req, args.reply_to, args.note)

    # Dedupe on (req, status) HERE, at the source. agent-vm dedupes too, but a
    # duplicate it has to absorb is still a bug: the consumer's own write to the
    # queue re-fires its path unit, so without this every pass would republish
    # `accepted`.
    st = load_state()
    key = f"{args.req}|{args.status}"
    if key in st:
        print(f"queue-reply: {key} already published at {st[key]} — not resending")
        return 0

    body = json.dumps({"v": 1, "req": args.req, "reply_to": args.reply_to,
                       "status": args.status, "note": note},
                      separators=(",", ":"), sort_keys=False)
    if "\n" in body:
        die("body must be exactly one line")

    if args.dry_run:
        print(f"queue-reply: DRY RUN -> {URL}\n  {body}")
        return 0

    req = urllib.request.Request(
        URL, data=body.encode(),
        headers={"Title": f"queue {args.status} {args.req}",
                 "Tags": args.status,
                 "Authorization": f"Bearer {token()}",
                 "Content-Type": "application/json"})
    # Log the status ALWAYS and never swallow a failure (flakes#242): a reply
    # channel that drops a publish silently is worse than no channel, because
    # agent-vm would wait forever on an answer that was never sent.
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code = r.status
            r.read()
    except urllib.error.HTTPError as exc:
        print(f"queue-reply: PUBLISH FAILED http={exc.code} {key} "
              f"body={exc.read()[:200]!r}", file=sys.stderr)
        return 1
    except Exception as exc:                                  # noqa: BLE001
        print(f"queue-reply: PUBLISH FAILED {key}: {exc!r}", file=sys.stderr)
        return 1

    print(f"queue-reply: published http={code} {key} note={note or '-'}")
    if code // 100 != 2:
        print(f"queue-reply: non-2xx http={code} for {key}", file=sys.stderr)
        return 1

    from datetime import datetime, timezone
    st[key] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
