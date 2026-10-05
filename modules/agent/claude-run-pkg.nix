# claude-run — run a headless `claude -p` and refuse to pass an auth error off
# as a result.
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
# Usage:  claude-run <timeout> <prompt-file>   # stdout = claude's stdout
#   exit 0  ok
#   exit 2  the login is the problem — re-auth, do not debug the job.
#           ⚠️ On exit 2 stdout is deliberately EMPTY, so a caller that pipes it
#           somewhere cannot publish the error as content the way newsdesk did.
#   exit 1  anything else
{ pkgs }:
pkgs.writeShellApplication {
  name = "claude-run";
  runtimeInputs = [ pkgs.coreutils pkgs.gnugrep pkgs.python3 ];
  # writeShellApplication adds `set -o errexit`, so claude's status is captured
  # explicitly rather than left to `&&` / `||` chaining.
  text = ''
    if [ "$#" -ne 2 ]; then
      echo "claude-run: usage: claude-run <timeout> <prompt-file>" >&2
      exit 1
    fi
    limit="$1"; promptFile="$2"
    if [ ! -r "$promptFile" ]; then
      echo "claude-run: prompt file not readable: $promptFile" >&2
      exit 1
    fi

    # Cheap deterministic pre-check: an already-dead refresh token cannot be a
    # job bug, so say so before spending 20 minutes finding out. The field is
    # epoch MILLISECONDS -- read as seconds it dates to 1970 and fires forever.
    cred="$HOME/.claude/.credentials.json"
    if [ ! -r "$cred" ]; then
      echo "claude-run: no readable credentials at $cred — run 'claude' interactively on gromit." >&2
      exit 2
    fi
    if ! python3 -c '
import json,sys,time
d=json.load(open(sys.argv[1]))["claudeAiOauth"]
sys.exit(1 if d["refreshTokenExpiresAt"]/1000 <= time.time() else 0)
' "$cred" 2>/dev/null; then
      echo "claude-run: refresh token has EXPIRED — run 'claude' interactively on gromit to re-auth." >&2
      exit 2
    fi

    # Run it. stdout is buffered to a file rather than streamed, because it has
    # to be INSPECTED before any caller is allowed to treat it as a result.
    outFile="$(mktemp)"; errFile="$(mktemp)"
    trap 'rm -f "$outFile" "$errFile"' EXIT
    set +o errexit
    timeout "$limit" claude -p "$(cat "$promptFile")" >"$outFile" 2>"$errFile"
    rc=$?
    set -o errexit

    cat "$errFile" >&2   # the journal keeps stderr either way

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
    # test-case list below in step.
    bytes=$(wc -c < "$outFile")
    if grep -qiF 'Failed to authenticate' "$outFile" \
       || { [ "$bytes" -lt 400 ] && grep -qiE 'oauth|session expired|login expired|could not be refreshed|please log ?in|run /login|unauthor|authentication_failed' "$outFile"; }; then
      echo "claude-run: LOGIN EXPIRED — claude printed an auth error to stdout (exit $rc, $bytes bytes):" >&2
      sed 's/^/claude-run:   /' "$outFile" >&2
      echo "claude-run: run 'claude' interactively on gromit to re-auth. Output withheld so it cannot be published as a result." >&2
      exit 2
    fi

    cat "$outFile"
    [ "$rc" -eq 0 ] || exit 1
    exit 0
  '';
}
