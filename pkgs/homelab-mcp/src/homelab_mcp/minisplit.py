"""Mini split (heat pump) control, over the `minisplit` CLI.

A transport, like cameras.py: the CLI holds the Tuya local key, speaks the
protocol and READ-BACK-VERIFIES every write, so none of that is reimplemented
here. This module only builds an argument list and parses JSON.

SCOPE: read state; set power, setpoint and mode. That is the whole surface.
Deliberately NOT here:

  * the cloud gate (`minisplit-cloud`). It changes a firewall and needs root,
    which this sandboxed service cannot and should not have. Ask the agent.
  * `raw`, i.e. arbitrary data points. Thirty of them are undocumented, and an
    unknown DP written from a phone is a good way to put a heat pump into a
    state nobody can name.

Writing a setpoint IS a physical action in Chris's own house. That's the point
of the tool; it reaches nothing else.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

MODES = ("auto", "cool", "dry", "fan", "heat")
MIN_F, MAX_F = 60.0, 86.0  # same bounds the CLI enforces; checked here first


class MiniSplitError(RuntimeError):
    """The unit could not be reached, or refused/ignored the request."""


def _run(binary: str, args: list[str], timeout: int = 45) -> dict[str, Any]:
    if not shutil.which(binary):
        raise MiniSplitError(f"{binary} is not on PATH; mini split tools are not available here")
    try:
        proc = subprocess.run(  # noqa: S603 - list form, no shell
            [binary, "--json", *args], capture_output=True, text=True,
            timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MiniSplitError(f"minisplit timed out after {timeout}s; the unit is probably unreachable") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise MiniSplitError(detail or f"minisplit exited {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MiniSplitError("minisplit returned output that was not JSON") from exc


def status(binary: str) -> dict[str, Any]:
    return _run(binary, ["status"])


def plan(power: bool | None, temp_f: float | None, mode: str | None) -> list[list[str]]:
    """Validate a request and turn it into CLI calls, in a safe order.

    Mode and setpoint go BEFORE power-on (so the unit starts in the requested
    state) and power-off goes first (so nothing else runs on a unit being shut
    down needlessly). Raises before anything is sent if any part is invalid,
    so a request is never half-applied because of a typo in its last field.
    """
    if power is None and temp_f is None and mode is None:
        raise MiniSplitError("nothing to change: give power, temp_f and/or mode")
    if mode is not None and mode not in MODES:
        raise MiniSplitError(f"mode must be one of {', '.join(MODES)}")
    if temp_f is not None and not MIN_F <= temp_f <= MAX_F:
        raise MiniSplitError(f"temp_f must be {MIN_F:g}-{MAX_F:g}")
    calls: list[list[str]] = []
    if power is False:
        calls.append(["off"])
    if mode is not None:
        calls.append(["mode", mode])
    if temp_f is not None:
        calls.append(["temp", f"{temp_f:g}"])
    if power is True:
        calls.append(["on"])
    return calls


def apply(binary: str, power: bool | None, temp_f: float | None, mode: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for args in plan(power, temp_f, mode):
        result = _run(binary, args)
    return result
