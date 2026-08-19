#!/usr/bin/env bash
# content-archives-refresh — weekly rebuild of the podcast transcript archives.
#
# Closes the open loop from the original archive build (2026-06): the corpora
# were built and pushed, but nothing kept them current. On 2026-08-18 that cost
# a real answer — a question about a tool discussed on Linux Unplugged returned
# NOTHING, because the archive had silently stopped at episode 670 two months
# earlier. A stale archive answers "not found" in exactly the same words as an
# archive that genuinely lacks the thing.
#
# Per archive: pull --ff-only -> build.py -> commit the generated paths -> push.
#
# FAILURE POLICY, which is the interesting part:
#   - a transient failure (feed down, network blip) exits 0. One bad week must
#     not trip the SystemdUnitFailed alert.
#   - a PERSISTENT failure exits 1 on purpose. If an archive has not refreshed
#     successfully within $STALE_DAYS, that is no longer transient, and the
#     existing failed-unit alerting should see it. This is the whole point:
#     an updater that quietly stops working is indistinguishable from a show
#     that stopped releasing episodes, and only one of those is fine.
set -uo pipefail

STAMP_DIR=${STATE_DIRECTORY:-/var/lib/content-archives}
STALE_DAYS=${STALE_DAYS:-21}
ARCHIVES=${ARCHIVES:-}

log() { echo "content-archives: $*"; }

# Only the paths build.py generates AND git tracks. Deliberately NOT
# `git add -A`: these checkouts are the agent's working clones and may hold
# unrelated files (embeddings.db from the semantic-search work, hand-written
# curation notes). A blanket add would sweep those into a machine commit.
#
# transcripts.db is deliberately ABSENT: both archives .gitignore it, because
# the FTS5 index is regenerable from the markdown. Listing it here was a real
# bug — `git add a b ignored` fails ATOMICALLY, staging nothing at all, so the
# unit would have committed nothing every week while reporting success.
GENERATED=(episodes PICKS.md)

refresh_one() {
  local name=$1 path=$2 out newcount stamp
  stamp="$STAMP_DIR/$name.stamp"

  if [ ! -d "$path/.git" ]; then
    log "$name: $path is not a git checkout — skipping"
    return 1
  fi
  if ! git -C "$path" remote get-url origin >/dev/null 2>&1; then
    log "$name: no 'origin' remote configured — skipping"
    return 1
  fi

  # These are the AGENT's working clones, so they may legitimately be sitting on
  # a feature branch. Pushing HEAD:main from one would publish whatever that
  # branch happens to be. Refuse rather than guess.
  local branch
  branch=$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ "$branch" != "main" ]; then
    log "$name: checked out on '$branch', not main — skipping (refusing to push a feature branch to main)"
    return 1
  fi

  # A brand-new archive has no origin/main yet. Demanding one would mean a
  # freshly-added show could never bootstrap itself onto the server.
  if git -C "$path" rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
    # --ff-only is the fail-safe: if the tree is dirty or has diverged we skip
    # rather than manufacture a merge commit in someone's archive.
    #
    # `origin main` is spelled out on purpose. A bare `git pull` needs upstream
    # TRACKING, which a freshly-bootstrapped archive does not have — so the run
    # after a bootstrap failed with "dirty or diverged" and the archive would
    # have quietly rotted to STALE three weeks later.
    if ! git -C "$path" pull --quiet --ff-only origin main 2>&1; then
      log "$name: pull skipped (dirty or diverged) — not refreshing"
      return 1
    fi
  else
    log "$name: origin has no main yet — bootstrapping"
  fi

  if ! out=$(cd "$path" && python3 build.py 2>&1); then
    log "$name: build.py failed"
    printf '%s\n' "$out" | tail -5
    return 1
  fi
  printf '%s\n' "$out" | tail -3

  # build.py prints e.g. "Done. 10 new, 73 existing, 83 indexed."
  newcount=$(printf '%s' "$out" | grep -oE 'Done\. [0-9]+ new' | grep -oE '[0-9]+' | head -1)
  newcount=${newcount:-0}

  # A successful build with nothing new is still a success — the show simply
  # had no new episodes. Stamp it so freshness tracks the CHECK, not the feed.
  date +%s > "$stamp"

  # One path at a time: a single unexpected/ignored path must not abort the
  # whole staging step and leave us silently committing nothing.
  local g
  for g in "${GENERATED[@]}"; do
    [ -e "$path/$g" ] || continue
    git -C "$path" add -- "$g" 2>/dev/null || log "$name: could not stage $g (ignored?)"
  done

  if git -C "$path" diff --cached --quiet; then
    [ "$newcount" -gt 0 ] && \
      log "$name: $newcount new reported but no tracked changes to commit"
  else
    git -C "$path" -c user.name=ww4-bot -c user.email=bot@rosemaryacres.com \
      commit --quiet -m "archive: weekly refresh — $newcount new episode(s)" || {
        log "$name: commit failed"; return 1; }
  fi

  # Push whenever we are ahead of the server — NOT merely when this run found
  # something. Two cases depend on it: bootstrapping an archive whose origin has
  # no main yet, and retrying a commit whose push failed on an earlier run.
  # Gating the push on newcount stranded that commit forever, because the next
  # run finds nothing new and returns early.
  local ahead=0
  if ! git -C "$path" rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
    ahead=1
  else
    ahead=$(git -C "$path" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
  fi

  if [ "$ahead" -eq 0 ]; then
    log "$name: up to date with origin (no new episodes)"
    return 0
  fi

  if ! git -C "$path" push --quiet origin HEAD:main 2>&1; then
    log "$name: push failed ($ahead commit(s) local; next run will retry)"
    return 1
  fi
  log "$name: pushed ($ahead commit(s), +$newcount episode(s) this run)"
  return 0
}

main() {
  mkdir -p "$STAMP_DIR"
  local failed=0 stale=0 now entry name path stamp age

  now=$(date +%s)
  for entry in $ARCHIVES; do
    name=${entry%%:*}
    path=${entry#*:}
    log "--- $name ($path)"
    refresh_one "$name" "$path" || failed=$((failed + 1))
  done

  # Persistent-failure escalation. A single bad week is quiet; an archive that
  # has not refreshed in STALE_DAYS is a broken updater, and saying nothing
  # about it would repeat exactly the failure this tool exists to fix.
  for entry in $ARCHIVES; do
    name=${entry%%:*}
    stamp="$STAMP_DIR/$name.stamp"
    if [ ! -f "$stamp" ]; then
      log "$name: NEVER refreshed successfully"
      stale=$((stale + 1))
      continue
    fi
    age=$(( (now - $(cat "$stamp")) / 86400 ))
    if [ "$age" -gt "$STALE_DAYS" ]; then
      log "$name: STALE — last successful refresh was ${age}d ago (limit ${STALE_DAYS}d)"
      stale=$((stale + 1))
    fi
  done

  if [ "$stale" -gt 0 ]; then
    log "FAILING the unit: $stale archive(s) persistently stale. This is deliberate —"
    log "a silently-dead updater reads exactly like a show with no new episodes."
    return 1
  fi
  if [ "$failed" -gt 0 ]; then
    log "$failed archive(s) failed this run; within the ${STALE_DAYS}d grace window, exiting 0"
  fi
  return 0
}

main "$@"
