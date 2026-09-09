"""Customer NVR status, over the `blueiris` CLI.

This shells out to `blueiris --json` rather than reimplementing the Blue Iris
JSON API. That is deliberate: the login is an MD5 challenge
(`md5("user:session:password")`, single pass, no realm — NOT HTTP digest) and a
second implementation of it would be a second thing to get wrong and a second
place holding customer credentials. The CLI is tested against the live NVR; this
module is a transport.

SCOPE — read status, control alerting. Deliberately NOT exposed here:

  * `snapshot` / `snapshot-all`. This MCP server is reachable from the public
    internet (Anthropic IP range + bearer token + secret path prefix). Camera
    STATUS is operational metadata about equipment Chris maintains; camera
    IMAGERY is the inside of a third party's business. Those are different
    consent questions, and the frames stay on the box. Snapshots remain a CLI
    command run on gromit.
  * `log` and `status`. NVR system logs carry paths, user names and client IPs.
    Nothing needs them from a phone.

Mute is included even though it is a write, because it only ever silences
CHRIS'S OWN notifications — it changes nothing on the customer's system. The
moment he wants it is standing in front of a camera he cannot replace today.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

# A camera identifier is `Cam3` / `Cam26` — the NVR's short name. Constrained
# because these values are passed to a subprocess: no shell is involved (the
# call is a list, never a string), but a tight whitelist means a malformed
# identifier fails here with a clear message rather than inside the CLI's
# argument parser.
_MAX_REASON = 200


class CameraError(RuntimeError):
    """The NVR could not be reached, or the CLI refused the request."""


def _short_ok(short: str) -> bool:
    return bool(short) and len(short) <= 32 and all(
        c.isalnum() or c in "-_" for c in short)


def _run(binary: str, args: list[str], timeout: int = 60) -> str:
    if not shutil.which(binary):
        raise CameraError(
            f"{binary} is not on PATH; camera tools are not available here")
    try:
        proc = subprocess.run(  # noqa: S603 - list form, no shell
            [binary, *args], capture_output=True, text=True, timeout=timeout,
            check=False)
    except subprocess.TimeoutExpired as exc:
        raise CameraError(
            f"blueiris timed out after {timeout}s — the NVR is probably "
            f"unreachable") from exc
    if proc.returncode != 0:
        # stderr carries the CLI's own diagnosis ("... unreachable: Connection
        # refused"), which is far more useful than a generic failure. An
        # unreachable NVR is an ERROR, never an empty all-clear.
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise CameraError(detail or f"blueiris exited {proc.returncode}")
    return proc.stdout


def camera_status(binary: str, site: str, only_down: bool) -> dict[str, Any]:
    out = _run(binary, ["--site", site, "--json",
                        "offline" if only_down else "cams"])
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise CameraError("blueiris returned output that was not JSON") from exc


def list_muted(binary: str, site: str) -> dict[str, Any]:
    out = _run(binary, ["--site", site, "--json", "muted"], timeout=15)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise CameraError("blueiris returned output that was not JSON") from exc


def mute(binary: str, site: str, short: str, reason: str, days: int) -> str:
    if not _short_ok(short):
        raise CameraError(
            f"{short!r} is not a camera identifier (expected e.g. 'Cam26')")
    if days < 1 or days > 365:
        # No --forever from the MCP. A mute with no expiry is how a camera stays
        # dead for a year unnoticed; setting one should take a deliberate act at
        # a keyboard, not a sentence typed on a phone.
        raise CameraError(
            "days must be 1-365; indefinite mutes are CLI-only on purpose")
    return _run(binary, ["--site", site, "mute", short, "--days", str(days),
                         reason[:_MAX_REASON]], timeout=15).strip()


def unmute(binary: str, site: str, short: str) -> str:
    if not _short_ok(short):
        raise CameraError(
            f"{short!r} is not a camera identifier (expected e.g. 'Cam26')")
    # Exit 1 means "was not muted", which is information, not a failure.
    try:
        return _run(binary, ["--site", site, "unmute", short],
                    timeout=15).strip()
    except CameraError as exc:
        if "was not muted" in str(exc):
            return f"{short} was not muted"
        raise
