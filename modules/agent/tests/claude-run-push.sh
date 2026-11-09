#!/usr/bin/env bash
# Behavioural tests for claude-run, run at BUILD time (see claude-run-pkg.nix:
# the package every caller references is a symlink produced only if this
# passes, so an unproven wrapper cannot deploy).
#
# `claude` itself is stubbed. That is the point: every behaviour worth testing
# here is about what the WRAPPER does with what claude printed and whether it
# exited — and the real binary can neither be run in the sandbox nor made to
# produce a dead login on demand.
#
# ⚠️ The cases below mirror the auth-string list in claude-run-pkg.nix. An
# observed error string is one sample, not the set — when that list grows, grow
# these with it.
set -uo pipefail

CLAUDE_RUN="${1:?usage: claude-run-push.sh <path to claude-run>}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/bin" "$work/home/.claude"

# A live-looking credential: the wrapper's pre-check rejects an expired refresh
# token before running anything, and the field is epoch MILLISECONDS.
python3 - "$work/home/.claude/.credentials.json" <<'PY'
import json, sys, time
json.dump({"claudeAiOauth": {"refreshTokenExpiresAt": int((time.time() + 30*86400) * 1000)}},
          open(sys.argv[1], "w"))
PY

# The shebang is resolved from the running shell, not hardcoded: /usr/bin/env
# does not exist in the Nix build sandbox, and a stub that cannot execute fails
# every case below for the wrong reason.
{ echo "#!$(command -v bash)"; cat <<'STUB'
# Stub `claude -p`. Reads $SCENARIO, counts its own invocations, and learns the
# done-file path the way the real agent would: out of the appended system prompt.
set -uo pipefail
n=$(( $(cat "$COUNTF" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$COUNTF"
printf 'INVOKE %s\n' "$(printf '%s ' "$@" | tr '\n' ' ')" >> "$ARGLOG"

done_path=""
for a in "$@"; do
  case "$a" in
    *"DONE PROTOCOL"*)
      done_path="$(printf '%s\n' "$a" | sed -n 's/^  \(\/.*\)$/\1/p' | head -1)" ;;
  esac
done

say_done() { [ -n "$done_path" ] && printf '%s' "$1" > "$done_path"; }

case "$SCENARIO" in
  ok)
    echo "iteration $n output" ;;
  auth)
    echo "Failed to authenticate: OAuth session expired and could not be refreshed" ;;
  done-first)
    echo "iteration $n output"
    say_done '{"done": true, "why": "did the thing first time"}' ;;
  done-second)
    echo "iteration $n output"
    [ "$n" -ge 2 ] && say_done '{"done": true, "why": "needed one nudge"}' ;;
  never)
    echo "iteration $n output" ;;
  blocked)
    echo "iteration $n output"
    say_done '{"done": true, "why": "BLOCKED: needs a Cloudflare token only Chris can mint"}' ;;
  fenced)
    echo "iteration $n output"
    say_done '```json
{"done": true, "why": "wrapped the object in a fence"}
```' ;;
  plan-not-done)
    # The failure the done protocol exists to catch: it announces a plan and
    # stops. A done file that is not an explicit true must NOT end the run.
    echo "iteration $n output"
    [ "$n" -lt 3 ] && say_done '{"done": false, "why": "here is my plan"}'
    [ "$n" -ge 3 ] && say_done '{"done": true, "why": "actually finished on the third"}' ;;
  empty-why)
    echo "iteration $n output"
    say_done '{"done": true, "why": "   "}' ;;
  auth-late)
    echo "iteration $n output"
    [ "$n" -ge 2 ] && echo "Login expired · Please run /login" ;;
  slow-never)
    echo "iteration $n output"
    sleep 20 ;;
esac
exit 0
STUB
} > "$work/bin/claude"
chmod +x "$work/bin/claude"

export PATH="$work/bin:$PATH"
export HOME="$work/home"

pass=0; fail=0
run() {  # run <scenario> <args...>; sets RC, OUT, ERR, N
  SCENARIO="$1"; shift
  export SCENARIO
  COUNTF="$work/count"; ARGLOG="$work/args"
  export COUNTF ARGLOG
  : > "$COUNTF"; : > "$ARGLOG"
  OUT="$("$CLAUDE_RUN" "$@" 2>"$work/err")"; RC=$?
  ERR="$(cat "$work/err")"
  N="$(cat "$COUNTF")"
}

check() {  # check <name> <condition-description> <actual> <expected>
  if [ "$3" = "$4" ]; then
    pass=$((pass+1))
  else
    fail=$((fail+1))
    echo "FAIL: $1 — $2: expected [$4], got [$3]" >&2
    [ -n "${ERR:-}" ] && printf 'stderr was:\n%s\n' "$ERR" >&2
  fi
}

prompt="$work/prompt.md"
echo "do the thing" > "$prompt"

# ── default mode must behave exactly as it did before push mode existed ─────
run ok 5m "$prompt"
check "default/ok" "exit"        "$RC"  "0"
check "default/ok" "stdout"      "$OUT" "iteration 1 output"
check "default/ok" "invocations" "$N"   "1"
# …including passing NO session or system-prompt flags: the old call shape is
# part of the contract, since a document job's output must not change.
if grep -q -- "--session-id\|--append-system-prompt\|--resume" "$work/args"; then
  echo "FAIL: default/ok — passed a push-mode flag to claude" >&2; fail=$((fail+1))
else
  pass=$((pass+1))
fi

run auth 5m "$prompt"
check "default/auth" "exit"            "$RC"  "2"
check "default/auth" "stdout withheld" "$OUT" ""
case "$ERR" in *"LOGIN EXPIRED"*) pass=$((pass+1)) ;;
  *) echo "FAIL: default/auth — stderr does not name the login" >&2; fail=$((fail+1)) ;; esac

# ── push mode ───────────────────────────────────────────────────────────────
run done-first --push=3 5m "$prompt"
check "push/done-first" "exit"        "$RC"  "0"
check "push/done-first" "invocations" "$N"   "1"
check "push/done-first" "stdout"      "$OUT" "iteration 1 output"

run done-second --push=3 5m "$prompt"
check "push/done-second" "exit"        "$RC" "0"
check "push/done-second" "invocations" "$N"  "2"
check "push/done-second" "stdout concatenated" \
  "$OUT" "iteration 1 output
iteration 2 output"
# iteration 1 opens the session, iteration 2 resumes THAT id — not --continue,
# which would resume whatever ran last in the working directory.
sid1="$(grep '^INVOKE' "$work/args" | sed -n '1p' | grep -oE -- '--session-id [0-9a-f-]+' | awk '{print $2}')"
sid2="$(grep '^INVOKE' "$work/args" | sed -n '2p' | grep -oE -- '--resume [0-9a-f-]+' | awk '{print $2}')"
check "push/done-second" "resumes the same session id" "$sid2" "$sid1"
[ -n "$sid1" ] || { echo "FAIL: push/done-second — no --session-id on iteration 1" >&2; fail=$((fail+1)); }

run never --push=2 5m "$prompt"
check "push/never" "exit 3 (gave up, not broke)" "$RC" "3"
check "push/never" "invocations = 1 + limit"     "$N"  "3"
check "push/never" "output still emitted on 3" "$OUT" "iteration 1 output
iteration 2 output
iteration 3 output"

run blocked --push=3 5m "$prompt"
check "push/blocked" "exit"        "$RC" "0"
check "push/blocked" "invocations" "$N"  "1"
case "$ERR" in *"finished BLOCKED"*) pass=$((pass+1)) ;;
  *) echo "FAIL: push/blocked — stderr does not flag BLOCKED" >&2; fail=$((fail+1)) ;; esac

run fenced --push=3 5m "$prompt"
check "push/fenced" "a fenced object still counts" "$RC" "0"
check "push/fenced" "invocations"                  "$N"  "1"

run plan-not-done --push=4 5m "$prompt"
check "push/plan-not-done" "done:false does not finish" "$RC" "0"
check "push/plan-not-done" "continued until a real done" "$N" "3"

run empty-why --push=1 5m "$prompt"
check "push/empty-why" "a reasonless done is not a done" "$RC" "3"
check "push/empty-why" "invocations"                     "$N"  "2"

# The one that made the output aggregate rather than stream: a lapse on
# iteration 2 must still leave stdout EMPTY, or exit 2 publishes half a result.
run auth-late --push=3 5m "$prompt"
check "push/auth-late" "exit"            "$RC"  "2"
check "push/auth-late" "stdout withheld" "$OUT" ""

# Budget is for the WHOLE run: 60s, one 20s iteration, then under the 45s floor.
run slow-never --push=5 75s "$prompt"
check "push/budget" "exit 3"                       "$RC" "3"
check "push/budget" "looped, then stopped on budget rather than the limit of 5" "$N" "2"
case "$ERR" in *"OUT OF BUDGET"*) pass=$((pass+1)) ;;
  *) echo "FAIL: push/budget — stderr does not say OUT OF BUDGET" >&2; fail=$((fail+1)) ;; esac

# ── argument handling ───────────────────────────────────────────────────────
run ok --push=abc 5m "$prompt"
check "args/bad-push" "exit 1" "$RC" "1"
run ok --push 30s "$prompt"
check "args/short-budget" "a budget too small to continue is refused" "$RC" "1"
run ok --push=2 banana "$prompt"
check "args/unparseable-budget" "exit 1" "$RC" "1"
run ok --nope 5m "$prompt"
check "args/unknown-flag" "exit 1" "$RC" "1"
run ok 5m "$work/no-such-file"
check "args/missing-prompt" "exit 1" "$RC" "1"

echo "claude-run tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
