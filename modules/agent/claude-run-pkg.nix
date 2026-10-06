# claude-run — run a headless `claude -p` and refuse to pass an auth error off
# as a result. With --push, also refuse to let it stop early.
#
# Shared by daybook.nix, digest.nix and podcast-triage.nix, following the
# notify-pkg.nix pattern (`import ./claude-run-pkg.nix { inherit pkgs; }`).
#
# ⚠️ THE FAILURE IS ON STDOUT, AND IT EXITS 0. This is documented in this repo
# already: on 2026-09-04 and 09-05 `claude -p` printed
#
#     Failed to authenticate: OAuth session expired and could not be refreshed
#
# to STDOUT, the newsdesk caller redirected stdout into the edition body, and
# because a 73-byte error is not empty the emptiness guard declined. Two editions
# were published as that error — 0 items, no notification, no trace. See
# newsdesk/newsdesk/edition.py:256 and its regression test.
#
# So neither `2>/dev/null` nor `|| fallback` was ever going to catch this:
# stderr was not where the message went, and the exit status was 0. Today a
# logged-out daybook finds no `^TLDR:` line in that error and reports
# "finished (no TLDR line)" — a soft success. Chris's words: "Its annoying to
# find out I'm logged out because a bunch of auto-run commands start failing."
#
# ── PUSH MODE (--push), added 2026-10-05 ────────────────────────────────────
# The second way a scheduled run fails quietly: it stops before the work is
# done and exits 0, so the job reports success for a task it abandoned. The
# idea is lifted from Jupiter Extras' "Clanker Therapy 2" (2026-10-02), where
# Wes describes the version he built — and it is worth saying why it is shaped
# this way, because the obvious implementation is the bad one:
#
#   Chris:  the /goal plugins "are just adding a ton of more crap to my context
#           because it's just this big set of instructions that just bully the
#           LLM into keep going, keep going" — and so he hits compaction MORE.
#   Wes:    instead give it a tool it must CALL to be finished, "and it has to
#           submit a message explaining why it's done", and the HARNESS says
#           continue until it does.
#
# So the nagging lives in the loop, not in the context. One short sentence of
# protocol is appended to the system prompt; the agent signals completion by
# writing a done file with a reason. Stopping becomes an explicit, argued act
# instead of the default. A BLOCKED reason is a legitimate way to finish, which
# the stream's version lacked — without it an impossible task burns every
# continuation before giving up.
#
# ⚠️ PUSH MODE IS FOR TASK JOBS, NOT DOCUMENT JOBS, and none of the three
# current callers turn it on. A document job (digest, daybook, triage) pipes
# stdout somewhere as the result; push mode CONCATENATES the output of every
# continuation, so turning it on there would publish "...continuing now" tails
# into the page. It is for a run whose product is an action — investigate,
# fix, open a PR — where quitting halfway is the failure.
#
# Usage:  claude-run [--push[=N]] <timeout> <prompt-file>
#   stdout = claude's stdout (in push mode: every iteration concatenated in
#            order, emitted once at the end — never streamed, see exit 2)
#   exit 0  ok
#   exit 2  the login is the problem — re-auth, do not debug the job.
#           ⚠️ On exit 2 stdout is deliberately EMPTY, so a caller that pipes it
#           somewhere cannot publish the error as content the way newsdesk did.
#   exit 3  push mode only: ran out of continuations (or budget) with no done
#           signal. DISTINCT from 1 on purpose — the job did not break, it
#           failed to finish, and those want different responses.
#   exit 1  anything else
#
# --push=N allows N continuations after the first run (N+1 invocations at most);
# bare --push means 3. The <timeout> is the budget for the WHOLE run, not per
# iteration: each `claude` gets what is left of it, so push mode can never
# overrun the caller's TimeoutStartSec no matter how many times it continues.
{ pkgs }:
let
  app = pkgs.writeShellApplication {
  name = "claude-run";
  runtimeInputs = [ pkgs.coreutils pkgs.gnugrep pkgs.python3 ];
  # writeShellApplication adds `set -o errexit`, so claude's status is captured
  # explicitly rather than left to `&&` / `||` chaining.
  text = ''
    usage() {
      echo "claude-run: usage: claude-run [--push[=N]] <timeout> <prompt-file>" >&2
    }

    # ── args ────────────────────────────────────────────────────────────────
    pushLimit=0
    if [ "$#" -gt 0 ]; then
      case "$1" in
        --push)   pushLimit=3; shift ;;
        --push=*) pushLimit="''${1#--push=}"; shift ;;
        --*)      echo "claude-run: unknown option: $1" >&2; usage; exit 1 ;;
      esac
    fi
    case "$pushLimit" in
      ""|*[!0-9]*) echo "claude-run: --push takes a non-negative integer, got: $pushLimit" >&2; exit 1 ;;
    esac

    if [ "$#" -ne 2 ]; then
      usage
      exit 1
    fi
    limit="$1"; promptFile="$2"
    if [ ! -r "$promptFile" ]; then
      echo "claude-run: prompt file not readable: $promptFile" >&2
      exit 1
    fi

    # Prefer the one-year token when it is present. Sourced per invocation and
    # never exported globally: it cannot do Remote Control or claude.ai
    # connectors, and it OUTRANKS the /login credential in any session that
    # reads it, so a profile-wide export would break all the herdr panes.
    # See modules/agent/claude-oauth-token-secret.nix.
    tokenFile=/run/secrets/claude-oauth-token
    usingToken=0
    tok=""
    if [ -r "$tokenFile" ]; then
      # PARSED, not sourced. `.` would execute the file as shell, and shellcheck
      # rightly refuses a non-constant source (SC1090) -- which fails the build,
      # since writeShellApplication runs shellcheck. Parsing is both buildable
      # and safer: a malformed secret can only be rejected, never run.
      tok="$(sed -n 's/^[[:space:]]*CLAUDE_CODE_OAUTH_TOKEN=//p' "$tokenFile" | head -1 | tr -d '"'"'"'\r ')"
      case "$tok" in
        sk-ant-oat*)
          export CLAUDE_CODE_OAUTH_TOKEN="$tok"
          usingToken=1
          ;;
        "")
          echo "claude-run: $tokenFile sets no CLAUDE_CODE_OAUTH_TOKEN — falling back to the interactive credential." >&2
          ;;
        sk-ant-api*)
          # Would bill per token against the API instead of the subscription.
          echo "claude-run: REFUSING the token in $tokenFile — that is an API key (sk-ant-api), not a subscription token (sk-ant-oat). Falling back to the interactive credential." >&2
          ;;
        *)
          echo "claude-run: REFUSING the token in $tokenFile — unrecognised prefix. Falling back to the interactive credential." >&2
          ;;
      esac
    fi

    # Cheap deterministic pre-check: an already-dead refresh token cannot be a
    # job bug, so say so before spending 20 minutes finding out. The field is
    # epoch MILLISECONDS -- read as seconds it dates to 1970 and fires forever.
    cred="$HOME/.claude/.credentials.json"
    if [ "$usingToken" = 1 ]; then
      : # the one-year token is in play; the 29-day refresh wall does not apply
    elif [ ! -r "$cred" ]; then
      echo "claude-run: no readable credentials at $cred — run 'claude' interactively on gromit." >&2
      exit 2
    elif ! python3 -c '
import json,sys,time
d=json.load(open(sys.argv[1]))["claudeAiOauth"]
sys.exit(1 if d["refreshTokenExpiresAt"]/1000 <= time.time() else 0)
' "$cred" 2>/dev/null; then
      echo "claude-run: refresh token has EXPIRED — run 'claude' interactively on gromit to re-auth." >&2
      exit 2
    fi

    # ── the auth-error detector, applied to EVERY iteration ─────────────────
    # The documented string first, then a narrower fallback for drift. The
    # fallback is bounded by SIZE: a real run is long, an auth error is ~73
    # bytes, so "short AND mentions auth" is a safe pattern and a long document
    # that happens to discuss OAuth is not caught by it.
    #
    # ⚠️ There is more than one message. Built from the newsdesk incident, this
    # pattern caught only ONE of the three Anthropic documents as failure text
    # (code.claude.com/docs/en/authentication and /errors):
    #   "Failed to authenticate: OAuth session expired and could not be refreshed"
    #   "Login expired · Please run /login"          <- was MISSED
    #   "Anthropic profile login expired"            <- was MISSED
    # An observed string is one sample, not the set. Keep this list and the
    # test-case list in tests/claude-run-push.sh in step.
    looks_like_auth_error() {
      local f="$1" n
      n=$(wc -c < "$f")
      if grep -qiF 'Failed to authenticate' "$f"; then
        return 0
      fi
      if [ "$n" -lt 400 ] \
         && grep -qiE 'session expired|login expired|could not be refreshed|please log ?in|run /login|unauthor|authentication_failed' "$f"; then
        return 0
      fi
      return 1
    }

    bail_auth() {
      local f="$1" rc="$2" n
      n=$(wc -c < "$f")
      echo "claude-run: LOGIN EXPIRED — claude printed an auth error to stdout (exit $rc, $n bytes):" >&2
      sed 's/^/claude-run:   /' "$f" >&2
      echo "claude-run: run 'claude' interactively on gromit to re-auth. Output withheld so it cannot be published as a result." >&2
      exit 2
    }

    outFile="$(mktemp)"; errFile="$(mktemp)"; doneFile="$(mktemp)"
    aggFile="$(mktemp)"
    # Born non-existent: mktemp creates it, and an EMPTY done file must not read
    # as a done signal. Remove it now so "exists and parses" is the only yes.
    rm -f "$doneFile"
    trap 'rm -f "$outFile" "$errFile" "$doneFile" "$aggFile"' EXIT

    # ── single-shot (the default, and byte-for-byte the old behaviour) ───────
    if [ "$pushLimit" -eq 0 ]; then
      set +o errexit
      timeout "$limit" claude -p "$(cat "$promptFile")" >"$outFile" 2>"$errFile"
      rc=$?
      set -o errexit
      cat "$errFile" >&2   # the journal keeps stderr either way
      if looks_like_auth_error "$outFile"; then
        bail_auth "$outFile" "$rc"
      fi
      cat "$outFile"
      [ "$rc" -eq 0 ] || exit 1
      exit 0
    fi

    # ── push mode ───────────────────────────────────────────────────────────
    # The timeout is the budget for the whole run. Parse it ONCE and fail loudly
    # if it cannot be parsed: silently guessing a budget is how a 20-minute job
    # becomes an unbounded one.
    case "$limit" in
      *[0-9]s) budget=$(( ''${limit%s} )) ;;
      *[0-9]m) budget=$(( ''${limit%m} * 60 )) ;;
      *[0-9]h) budget=$(( ''${limit%h} * 3600 )) ;;
      *[0-9])  budget=$(( limit )) ;;
      *)
        echo "claude-run: --push needs a timeout this script can do arithmetic on (300, 45s, 20m, 2h), got: $limit" >&2
        exit 1 ;;
    esac
    if [ "$budget" -lt 60 ]; then
      echo "claude-run: --push with a budget under 60s ($budget) cannot fit a run plus a continuation." >&2
      exit 1
    fi

    # One short paragraph, appended to the SYSTEM prompt so the caller's own
    # prompt stays exactly what the caller wrote (digest passes the literal
    # "/catch-up"; a prompt-body injection would break a slash command).
    protocol="DONE PROTOCOL — this run is supervised. A loop outside you will tell you to continue every time you stop, so stopping is not how you finish. You finish by writing this file:

  $doneFile

containing one JSON object and nothing else:

  {\"done\": true, \"why\": \"<one sentence: what you actually completed>\"}

Write it ONLY when the task is genuinely complete. Do not write it to announce a plan, report partial progress, or ask a question — you will simply be continued. If you are truly blocked and cannot proceed, that is also a finish: write {\"done\": true, \"why\": \"BLOCKED: <what you need and who must provide it>\"}. Prefer finishing the work over narrating it; you have at most $pushLimit continuation(s)."

    continueMsg="You stopped without writing the done file, so the task is not finished. Continue the work now. Write the done file only when it is genuinely complete (or BLOCKED, per the done protocol)."

    # A deterministic session id, rather than --continue. `claude -p --continue`
    # resumes "the most recent conversation in this directory" — two scheduled
    # jobs in the same WorkingDirectory would then continue each OTHER's
    # session, which is the kind of fault that only shows up under load.
    sid="$(cat /proc/sys/kernel/random/uuid)"
    deadline=$(( $(date +%s) + budget ))

    # Accepts the done file, leniently about fences (a model that writes
    # ```json around the object meant yes) and strictly about everything else:
    # done must be exactly true and why must be non-empty, or it is not a
    # finish. Prints the reason for the journal.
    read_done() {
      python3 -c '
import json,sys
try:
    t=open(sys.argv[1]).read()
except OSError:
    sys.exit(1)
t="\n".join(l for l in t.splitlines() if not l.strip().startswith("```")).strip()
try:
    d=json.loads(t)
except Exception:
    sys.exit(1)
if not isinstance(d,dict) or d.get("done") is not True:
    sys.exit(1)
why=str(d.get("why","")).strip()
if not why:
    sys.exit(1)
print(why[:400])
' "$doneFile" 2>/dev/null
    }

    attempt=0
    while :; do
      attempt=$(( attempt + 1 ))
      remaining=$(( deadline - $(date +%s) ))
      # Below a floor there is no point starting another iteration: it would be
      # killed mid-thought and its partial output appended as if it were work.
      if [ "$remaining" -lt 45 ]; then
        echo "claude-run: push mode OUT OF BUDGET after $(( attempt - 1 )) iteration(s) — ''${remaining}s left of ''${budget}s, no done signal." >&2
        cat "$aggFile"
        exit 3
      fi

      : > "$outFile"; : > "$errFile"
      set +o errexit
      if [ "$attempt" -eq 1 ]; then
        timeout "$remaining" claude -p \
          --session-id "$sid" \
          --append-system-prompt "$protocol" \
          "$(cat "$promptFile")" >"$outFile" 2>"$errFile"
      else
        timeout "$remaining" claude -p \
          --resume "$sid" \
          --append-system-prompt "$protocol" \
          "$continueMsg" >"$outFile" 2>"$errFile"
      fi
      rc=$?
      set -o errexit
      cat "$errFile" >&2

      # Checked per iteration, not just once up front: a session can lapse
      # between a 06:00 iteration and a 06:40 one, and an auth error appended
      # to the aggregate output is exactly what exit 2 exists to prevent.
      if looks_like_auth_error "$outFile"; then
        bail_auth "$outFile" "$rc"
      fi

      # Accumulated, NOT emitted yet. If a later iteration turns out to be an
      # auth error, exit 2 must still leave stdout empty — emitting iteration 1
      # as it happened would have published half a result alongside a dead
      # login, which is the newsdesk failure wearing a different hat.
      cat "$outFile" >> "$aggFile"

      if why="$(read_done)"; then
        echo "claude-run: DONE after $attempt iteration(s) (rc=$rc): $why" >&2
        case "$why" in
          BLOCKED:*) echo "claude-run: the run finished BLOCKED — it needs something it could not get." >&2 ;;
        esac
        cat "$aggFile"
        exit 0
      fi

      if [ "$attempt" -gt "$pushLimit" ]; then
        echo "claude-run: push mode gave up after $attempt iteration(s) (limit $pushLimit continuation(s)) — no done signal, last rc=$rc." >&2
        # Emitted on 3 but not on 2: the work genuinely happened and the caller
        # may want it, whereas an auth error is not a result at all.
        cat "$aggFile"
        exit 3
      fi
      echo "claude-run: iteration $attempt stopped with no done signal (rc=$rc) — continuing ($(( pushLimit - attempt + 1 )) left)." >&2
    done
  '';
  };
in
# What every caller actually references is this symlink, and it exists only if
# the behavioural tests passed — the netradio pattern (modules/services/
# netradio/default.nix:378), where the artefact the config consumes IS the test
# output, so nothing can deploy around a failing check.
#
# The tests stub `claude`. That is deliberate: every behaviour worth proving
# here is about what the wrapper does with what claude printed and whether it
# exited, and the real binary can neither run in the build sandbox nor be asked
# for a dead login on demand. ~20s of the run is a deliberate sleep (the
# whole-run budget case); it is cached like any other derivation.
pkgs.runCommand "claude-run"
  {
    nativeBuildInputs = with pkgs; [ bash python3 coreutils gnugrep gnused gawk ];
  }
  ''
    bash ${./tests/claude-run-push.sh} ${app}/bin/claude-run
    mkdir -p $out/bin
    ln -s ${app}/bin/claude-run $out/bin/claude-run
  ''
