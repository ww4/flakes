#!/usr/bin/env bash
# PreToolUse guard for the `claude` agent. Deployed root-owned (0555) by
# claude-harness.nix to /etc/claude-code/pretooluse-guard.sh and referenced from
# the managed settings hook — so the agent cannot edit or remove its own backstop.
#
# Programmatic backstop ABOVE the settings allow/deny lists: hard-deny destructive
# commands no matter how they're phrased, so a single typo or clever rephrase
# can't slip a `rm -rf` past. (The PocketOS "agent deleted a volume" failure mode.)
#
# Contract: reads the tool call as JSON on stdin; emits a JSON decision.
#   permissionDecision: "deny"  -> blocked outright
#                       "ask"   -> fall through to the normal permission prompt
#                       "allow" -> auto-approved (we DON'T use this; let settings decide)
# Requires `jq` on PATH (add to the claude user's packages).

set -euo pipefail

input="$(cat)"
tool="$(printf '%s' "$input"  | jq -r '.tool_name // empty')"
cmd="$(printf '%s' "$input"   | jq -r '.tool_input.command // empty')"
cwd="$(printf '%s' "$input"   | jq -r '.cwd // empty')"

# The agent's own sandbox. Files it creates live here; Chris's files do not
# (and are OS-protected — the agent is a non-root user that can't write them).
WORKSPACE="/home/claude"

deny() {
  jq -n --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# Only guard Bash here; other tools are governed by settings.json.
[ "$tool" = "Bash" ] || { printf '{}'; exit 0; }

# --- Operand isolation helpers -------------------------------------------
#
# Several fences below used to substring-match the WHOLE command line, which
# produced a steady stream of false denials on benign work: a `/dev/null`
# redirect (fixed in #95), a legitimate `/nix` path in a *separate* pipeline
# stage, a read-only `grep -ciE 'token|secret|passwd'` hitting the credential
# fence, and — the case that proves the point — a command containing no delete
# at all, denied because the heredoc *prose describing this guard* mentioned
# the tokens it matches on. A fence cannot distinguish a command from a
# description of a command unless it looks at command structure.
#
# These two helpers narrow what a fence sees. They are deliberately crude and
# FAIL CLOSED: when in doubt they keep more text, so a fence errs toward
# denying rather than allowing.

# strip_heredocs: remove heredoc BODIES (data, not commands).
# `cat > f <<'EOF' … EOF` — the body is content being written, so a path or a
# delete verb inside it is prose, not an operation. The delimiter line and
# everything up to the matching terminator go away; the command line itself
# (including the `cat > f` part) is kept.
#
# FAILS CLOSED on an unterminated heredoc: if a body opens and never closes,
# the original text is returned unchanged rather than swallowing everything
# after it. (Caught by the adversarial corpus — the first cut let
# `cat <<EOF\ntext\nrm -rf /etc/nixos` through, because with no terminator the
# delete looked like heredoc body.)
strip_heredocs() {
  printf '%s' "$1" | awk -v orig="$1" '
    BEGIN { term = ""; open = 0 }
    {
      if (term != "") {
        line = $0; gsub(/^[ \t]+|[ \t]+$/, "", line)
        if (line == term) { term = ""; open = 0 }
        next
      }
      if (match($0, /<<-?[ \t]*['"'"'"]?[A-Za-z_][A-Za-z0-9_]*['"'"'"]?/)) {
        t = substr($0, RSTART, RLENGTH)
        sub(/^<<-?[ \t]*/, "", t); gsub(/['"'"'"]/, "", t)
        term = t; open = 1
      }
      buf = buf $0 "\n"
    }
    END { if (open) printf "%s", orig; else printf "%s", buf }'
}

# normalize_ops: turn shell punctuation into spaces and pad the ends, so a verb
# is detectable regardless of how it is wrapped. `eval "rm -rf /etc"` and
# `x=$(rm -rf /etc)` both become ` rm -rf /etc `. Without this the delete
# trigger needed a literal leading space, so BOTH of those slipped past the
# original guard entirely — as did `shred /etc/hosts` (no leading space).
# Found by the adversarial corpus 2026-07-29; these were pre-existing holes,
# not regressions.
#
# Newlines become " ; " rather than disappearing: a command at the START of a
# line is still a command, and the old ` rm ` trigger needed a literal leading
# SPACE — so `…\nrm -rf /etc` matched nothing at all. Rendering the separator
# with spaces around it makes the verb detectable AND keeps `;` as a segment
# boundary for split_segments.
normalize_ops() {
  # Newline -> ';' via tr (sed treats a \001 escape as a back-reference, which
  # silently DELETED the separator and glued `<<EOF` onto the next command).
  printf ' %s ' "$(printf '%s' "$1" \
    | tr '\n\t' '; ' \
    | sed -e 's/[`"'"'"'(){}$]/ /g' -e 's/;/ ; /g')"
}

# split_segments: break a command line into individual command invocations on
# shell separators (; && || | and newline), one per line. A fence can then ask
# "does the segment that actually runs `rm` reference a protected path?"
# instead of "does any token anywhere on the line look scary?".
split_segments() {
  printf '%s' "$1" | sed -e 's/&&/\n/g' -e 's/||/\n/g' -e 's/;/\n/g' -e 's/|/\n/g'
}

# strip_quoted: blank out single- and double-quoted literals.
# A quoted string is DATA, never a command name. Without this, splitting on `|`
# tears a quoted regex apart: `grep -E 'token|secret|passwd' f` produced a
# synthetic segment that was literally the word `passwd`, which then matched
# the credential fence (2026-07-29). Only used for command-NAME matching; the
# path fences keep seeing quoted text, since a quoted path is still a path.
strip_quoted() {
  printf '%s' "$1" | sed -e "s/'[^']*'/''/g" -e 's/"[^"]*"/""/g'
}

# Command text with heredoc bodies removed — what the structural fences below
# analyse. The RAW $cmd is still used for the catastrophic patterns, which stay
# deliberately paranoid and whole-line.
cmd_nodoc="$(strip_heredocs "$cmd")"
# Punctuation-normalised view, used for verb DETECTION and for the delete path
# check. Quoting and command substitution no longer hide an operation.
cmd_norm="$(normalize_ops "$cmd_nodoc")"

# --- Catastrophic deletes — blocked everywhere, always ---
case "$cmd" in
  *"rm -rf /"|*"rm -fr /"|*"rm -rf / "*|*"rm -fr / "*|*"rm -rf /*"*|*"rm -rf ~"*|*"rm -fr ~"*|*'rm -rf $HOME'*)
    deny "Catastrophic delete pattern — never." ;;
esac

# --- Deletes (rm / find -delete): allowed only INSIDE the agent's workspace ---
# Chris asked that the agent be free to delete files it created (its scratch dir
# is $WORKSPACE) while files Chris created stay protected. Those live outside
# $WORKSPACE and are OS-protected anyway; this fences the agent in as defense in
# depth. Anything reaching outside, or run as root, still routes to a PR / Chris.
case "$cmd_norm" in
  *" rm "*|*" -delete "*|*" -delete"|*" unlink "*)
    case "$cmd_norm" in *" sudo "*) deny "Deleting as root is Chris-only — route via a flake PR." ;; esac
    # The SilverBullet space is shared agent-writable ground (Chris 2026-07-09:
    # full agency in the space, deletes included — the space git autosave is the
    # undo log). Scrub its path before the fence checks so space deletes pass
    # while everything ELSE under /var (and the rest) stays blocked.
    SPACE="/var/lib/silverbullet"
    # Only the segments that actually invoke a delete are examined for
    # protected paths. A `/nix` path in an unrelated pipeline stage of the same
    # command line is no longer the delete's business (2026-07-28 false denial).
    del_segs="$(split_segments "$cmd_norm" | grep -E '(^|[[:space:]])(rm|unlink)([[:space:]]|$)|-delete|-exec[[:space:]]+rm' || true)"
    # Fail closed: if segment-splitting somehow yielded nothing while the
    # trigger above matched, fall back to scanning the whole command.
    [ -n "$del_segs" ] || del_segs="$cmd_norm"
    scrub="${del_segs//"$SPACE"/__SPACE__}"
    # Redirections to /dev/null are I/O plumbing, not delete operands — scrub
    # them so `rm foo 2>/dev/null` doesn't trip the /dev fence below
    # (2026-06-16 false positive). Only redirect forms are scrubbed; a literal
    # operand like `rm /dev/null` keeps its bare "/dev" and is still denied.
    scrub="${scrub//2>\/dev\/null/}"
    scrub="${scrub//2> \/dev\/null/}"
    scrub="${scrub//&>\/dev\/null/}"
    scrub="${scrub//&> \/dev\/null/}"
    scrub="${scrub//>>\/dev\/null/}"
    scrub="${scrub//>\/dev\/null/}"
    scrub="${scrub//> \/dev\/null/}"
    case "$scrub" in
      *"/home/chris"*|*"/mnt"*|*"/etc"*|*"/var"*|*"/nix"*|*"/boot"*|*"/usr"*|*"/root"*|*"/srv"*|*"/opt"*|*"/sys"*|*"/proc"*|*"/dev"*|*"..")
        deny "That delete reaches outside your $WORKSPACE workspace — make the change via a flake PR, or ask Chris." ;;
    esac
    case "$cwd" in
      "$WORKSPACE"|"$WORKSPACE"/*|/tmp|/tmp/*|"$SPACE"|"$SPACE"/*) : ;;   # sandbox or the shared space -> OK (defer to allow)
      *) deny "Delete from a non-workspace directory (cwd=$cwd) — cd into $WORKSPACE, or ask Chris." ;;
    esac
    ;;
  *" shred "*|*" truncate "*) deny "In-place destroy (shred/truncate) — make the change via a flake PR, or ask Chris." ;;
esac

# --- Credential changes: match the COMMAND, not the word ---
# `passwd ` used to be a whole-line substring match, so a read-only
# `grep -ciE 'token|secret|passwd' file` — scanning a file to assess how
# sensitive it was — got denied as an "account/credential change"
# (2026-07-29). Scanning FOR credential words is the opposite of changing one.
# Now the check looks at the command word at the start of a segment.
cred_hit="$(split_segments "$(strip_quoted "$cmd_nodoc")" \
  | sed -e 's/^[[:space:]]*//' \
  | grep -E '^(sudo[[:space:]]+)?(userdel|useradd|usermod|passwd|chpasswd|gpasswd)([[:space:]]|$)' || true)"
[ -z "$cred_hit" ] || deny "Account/credential change — Chris only."
# The shadow file keeps a whole-line match: there is no benign reason to touch
# it, so a false positive there costs nothing and a miss costs a lot.
case "$cmd_nodoc" in *"/etc/shadow"*) deny "Account/credential change — Chris only." ;; esac

# --- Other irreversible / out-of-band-privilege ops ---
# These read $cmd_nodoc rather than $cmd so that prose inside a heredoc body
# can't trip them; the patterns themselves are unchanged.
case "$cmd_nodoc" in
  *mkfs*|*" dd "*|*"of=/dev/"*|*" wipefs"*|*" fdisk"*|*" parted"*) deny "Disk/format op — must be run by Chris." ;;
  *"nixos-rebuild switch"*|*"nixos-rebuild boot"*) deny "Applying config is comin's job after a PR merge to main — don't switch directly." ;;
  *"git push"*)
    # Match the PUSH REFSPEC, not the word "main" anywhere on the line. The old
    # rule was a whole-line substring match, so a chained `git switch main`, a
    # push to a non-main branch (`main:testing`), an `echo`/`curl` mentioning
    # main, or a heredoc body all tripped it. Scope to the push invocation: take
    # everything after the last "git push" up to the next shell separator.
    seg="${cmd##*git push}"
    seg="${seg%%[;&|]*}"
    seg="${seg%%$'\n'*}"
    # Deny only a push that actually writes to main (`:main` dst, or a bare
    # `main`/`origin main` target). Reading FROM main (`main:testing`) is fine.
    case "$seg" in
      *":main"|*":main "*|*" main"|*" main "*) deny "Direct push to main — open a PR; Chris merges." ;;
    esac
    # Force-push is fine to testing / feature branches (needed for testing resets)
    # but never to main. main is also branch-protected server-side (defense in
    # depth). NOTE: a refspec-less `git push -f` while checked out on main isn't
    # caught here by string alone — server branch protection covers that case.
    case "$seg" in
      *"--force"*|*" -f "*|*" -f")
        case "$seg" in
          *":main"|*":main "*|*" main"|*" main "*) deny "Force-push to main is never allowed." ;;
        esac ;;
    esac
    ;;
  # (credential changes moved above — they now match the invoked command
  # rather than the word appearing anywhere on the line)
  *">/dev/sd"*|*"> /dev/sd"*|*"> /dev/nvme"*) deny "Raw block-device write — Chris only." ;;
esac

# Everything else: defer to the settings allow/ask/deny lists and (if needed) the
# normal prompt. We intentionally do not auto-"allow" from here.
printf '{}'
exit 0
