#!/usr/bin/env bash
# Stop hook for the `claude` agent — AUTOMATIC SELF-IMPROVEMENT CHECKPOINT.
#
# Deployed root-owned (0555) by claude-harness.nix to
# /etc/claude-code/reflection-hook.sh and referenced from a `Stop` hook in the
# managed settings — so the agent cannot disable or edit its own reflection
# trigger. This is the deterministic "count to 15" half of the self-improvement
# loop (Hermes' 5th pillar): the agent captures friction continuously in the
# friction-log memory; THIS fires at session end and, on a sensible cadence,
# forces a brief reflection so lessons don't rot. See README.md + the /retro
# playbook.
#
# ─── SAFETY / DESIGN (read before changing the knobs) ───────────────────────
#  * This is a NUDGE, not a privilege. The worst case if it misfires is a little
#    wasted reflection; it grants nothing. The actual boundary is unchanged
#    (OS user + sudo allowlist + PR-merge gate). It is gated only because it
#    lives in the tamper-proof managed settings (allowManagedHooksOnly:true), so
#    any change to it is reviewed — appropriate for anything in /etc/claude-code.
#  * LOOP-SAFE: if Claude Code is already continuing because of this hook
#    (stop_hook_active=true) we exit immediately — no re-trigger.
#  * HEADLESS-SAFE: scheduled/non-interactive runs (e.g. the weekly digest)
#    export CLAUDE_AUTONOMOUS=1; we no-op then, so a `claude -p` job is never
#    derailed into doing reflection work. (/catch-up already surfaces friction.)
#  * AT MOST ONCE PER SESSION: once we've nudged in a session we don't nag again.
#  * The cadence state file (~/.claude/reflection-state) is agent-writable on
#    purpose — it's a reminder counter, not a security control. If the agent
#    resets it, it only reflects LESS, which hurts only itself.
#
# Contract: reads the Stop tool event as JSON on stdin; to FORCE a reflection we
# emit {"decision":"block","reason":"<checkpoint prompt>"} which makes the model
# continue with that instruction. To allow a normal stop we exit 0 silently.
# Requires `jq`.

set -euo pipefail

# ── tunables ────────────────────────────────────────────────────────────────
SESSION_N=8     # nudge after this many sessions since the last reflection...
OPEN_CAP=4      # ...or immediately once this many friction items pile up.
STATE="$HOME/.claude/reflection-state"

input="$(cat)"

# Loop guard: don't re-fire while the model is already continuing due to us.
active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false')"
[ "$active" = "true" ] && { exit 0; }

# Headless guard: never derail a scheduled / non-interactive run.
[ -n "${CLAUDE_AUTONOMOUS:-}" ] && exit 0

sid="$(printf '%s' "$input" | jq -r '.session_id // "unknown"')"

# Count OPEN friction items (lines like "- [..." under the "## Open" heading).
fl="$(find "$HOME/.claude/projects" -name friction-log.md 2>/dev/null | head -1 || true)"
open=0
if [ -n "$fl" ]; then
  open="$(awk '/^## Open/{f=1;next} /^## /{f=0} f && /^- \[/{c++} END{print c+0}' "$fl")"
fi

# Load cadence state (key=value; absent on first run).
session_count=0; last_session=""; last_reflect=0; reminded_in=""
if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  . "$STATE" 2>/dev/null || true
fi

# New session? bump the session counter.
if [ "$sid" != "$last_session" ]; then
  session_count=$((session_count + 1))
  last_session="$sid"
fi

# Already nudged in THIS session → record state and stop (no nagging).
if [ "$reminded_in" = "$sid" ]; then
  { echo "session_count=$session_count"; echo "last_session=$last_session"; \
    echo "last_reflect=$last_reflect"; echo "reminded_in=$reminded_in"; } > "$STATE"
  exit 0
fi

since=$((session_count - last_reflect))

trigger=0
reason=""
if [ "$open" -ge "$OPEN_CAP" ]; then
  trigger=1
  reason="Self-improvement checkpoint: the friction-log has ${open} OPEN items — that's enough to be worth distilling. Before you wrap up, run /retro: turn the open friction into fixes (a playbook in agent-toolkit, a memory lesson, a docs fix, or — for anything needing new power — a gated PR + an open-loops entry). Keep it honest and brief. If now genuinely isn't the moment, note why and continue."
elif [ "$open" -ge 1 ] && [ "$since" -ge "$SESSION_N" ]; then
  trigger=1
  reason="Self-improvement checkpoint: it's been ${since} sessions since the last reflection and there ${open} open friction-log item(s) waiting. Run /retro now — distill them into fixes you own (playbook/memory/docs) or a gated PR for anything needing approval. Brief and honest; skip with a one-line why if truly not the time."
fi

if [ "$trigger" = "1" ]; then
  reminded_in="$sid"
  last_reflect="$session_count"
  { echo "session_count=$session_count"; echo "last_session=$last_session"; \
    echo "last_reflect=$last_reflect"; echo "reminded_in=$reminded_in"; } > "$STATE"
  jq -n --arg r "$reason" '{decision:"block", reason:$r}'
  exit 0
fi

# No trigger — persist the counter and allow a normal stop.
{ echo "session_count=$session_count"; echo "last_session=$last_session"; \
  echo "last_reflect=$last_reflect"; echo "reminded_in=$reminded_in"; } > "$STATE"
exit 0
