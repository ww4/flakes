# media-mirror — guarded mirror of /mnt/fusion -> /mnt/backup/all
#
# Media is NEVER deleted automatically. `sync` only copies files and writes a
# review list; deletions happen only when you run `approve`, and even then the
# removed files are moved to a dated graveyard rather than destroyed.
#
# Subcommands:
#   sync                 copy new/changed files; queue any deletions for review
#   approve [<limit>]    apply queued deletions (moved to a dated graveyard);
#                        optional limit overrides MAX_DELETE for this run
#   recover [<date>]     restore files from a graveyard snapshot
#   prune-graveyard      delete graveyard snapshots past the retention window
#   preflight [which]    check pool drives (which: all | fusion | backup)
#   status               show current state
#
# (writeShellApplication already sets: set -o errexit -o nounset -o pipefail)

# Inherit the ERR trap into functions/subshells so unexpected failures alert.
set -o errtrace

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SRC=/mnt/fusion
DST=/mnt/backup/all

# Physical member drives of each mergerfs pool. Preflight verifies every one is
# mounted and carries its .pool-member sentinel before any rsync runs — this is
# what stops an offline drive from being mistaken for mass deletions.
FUSION_MEMBERS=(/mnt/primary/D1 /mnt/primary/D2 /mnt/primary/D3 /mnt/primary/D4 /mnt/primary/D5 /mnt/primary/D6)
BACKUP_MEMBERS=(/mnt/backup/D1 /mnt/backup/D2 /mnt/backup/D3 /mnt/backup/D4)

STATE=/var/lib/media-mirror
PENDING="$STATE/pending-deletions.txt"   # raw rsync --delete candidates
MOVED="$STATE/pending-moved.txt"         # candidates whose content moved on SRC
DELETED="$STATE/pending-deleted.txt"     # genuine deletions — the real review list
LOGDIR="$STATE/logs"
GRAVEYARD="$DST/.graveyard"
GRAVEYARD_RETENTION_DAYS=30

# Safety cap: approve aborts if more files than this are queued for deletion.
MAX_DELETE=250

# rsync excludes, anchored at the transfer root. Keep the restic repo, the
# graveyard, and the drive sentinels out of the mirror. /arr is tier-3
# content (mergerfs+snapraid parity protects it; re-download is the DR path)
# and is deliberately NOT mirrored — keeps the backup pool focused on tier 2.
EXCLUDES=(--exclude=/restic --exclude=/.graveyard --exclude=.pool-member --exclude=/arr --exclude=/pinchflat --exclude=/rick-offsite)
# ─────────────────────────────────────────────────────────────────────────────

# Notification failures must never abort a backup.
notify() { gromit-notify "$1" "$2" "${3:-default}" "${4:-}" || true; }

# Generic alert for unexpected (non-die) failures.
trap 'notify "Media mirror ERROR" "Unexpected failure — check: journalctl -u media-mirror-sync" urgent rotating_light' ERR

die() {
  echo "media-mirror: $1" >&2
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root — try: sudo media-mirror $1"
}

# preflight <all|fusion|backup> — returns non-zero if any member drive is
# missing. Prints offending drives to stderr.
preflight() {
  local which="${1:-all}" mp ok=1
  local members=()
  case "$which" in
    fusion) members=("${FUSION_MEMBERS[@]}") ;;
    backup) members=("${BACKUP_MEMBERS[@]}") ;;
    all)    members=("${FUSION_MEMBERS[@]}" "${BACKUP_MEMBERS[@]}") ;;
    *)      die "preflight: unknown pool '$which'" ;;
  esac
  for mp in "${members[@]}"; do
    if ! mountpoint -q "$mp"; then
      echo "  drive NOT MOUNTED: $mp" >&2; ok=0
    elif [ ! -f "$mp/.pool-member" ]; then
      echo "  sentinel MISSING:  $mp/.pool-member" >&2; ok=0
    fi
  done
  [ "$ok" -eq 1 ]
}

cmd_preflight() {
  if preflight "${1:-all}"; then
    echo "preflight OK — all pool members online"
  else
    die "preflight FAILED — a pool member drive is offline"
  fi
}

# Split $PENDING into moved (content still present on SRC) vs genuinely
# deleted. A move/rename keeps identical content, hence identical size+mtime,
# so this is metadata-only — no hashing — and stays fast across a multi-TB pool.
classify_deletions() {
  : > "$MOVED"
  : > "$DELETED"
  [ -s "$PENDING" ] || return 0

  # Fingerprint every source file as "size:mtime".
  local idx
  idx=$(mktemp)
  find "$SRC" -type f -printf "%s:%T@\n" 2>/dev/null | cut -d. -f1 | sort -u > "$idx" || true

  local relpath dstfile
  while IFS= read -r relpath; do
    case "$relpath" in */) continue ;; esac          # skip directory entries
    dstfile="$DST/$relpath"
    if [ -f "$dstfile" ] && grep -qxF "$(stat -c "%s:%Y" "$dstfile")" "$idx"; then
      echo "$relpath" >> "$MOVED"
    else
      echo "$relpath" >> "$DELETED"
    fi
  done < "$PENDING"
  rm -f "$idx"
}

cmd_sync() {
  require_root sync
  mkdir -p "$STATE" "$LOGDIR"

  if ! preflight all; then
    notify "Media mirror ABORTED — drive offline" \
"A mergerfs pool member is missing. No files were changed.
Run 'media-mirror status' and check the drives." \
      urgent rotating_light
    die "preflight failed — a pool member drive is offline"
  fi

  local ts log
  ts=$(date +%Y-%m-%d_%H%M%S)
  log="$LOGDIR/sync-$ts.log"

  # 1. Additive copy — never deletes anything.
  echo "additive sync: $SRC/ -> $DST/"
  rsync -aH --stats "${EXCLUDES[@]}" "$SRC/" "$DST/" | tee "$log"

  # 2. Compute what a --delete pass would remove; queue it for review.
  echo "computing deletions ..."
  local tmp
  tmp=$(mktemp)
  rsync -aH --delete --dry-run --itemize-changes "${EXCLUDES[@]}" \
    "$SRC/" "$DST/" > "$tmp"
  grep '^\*deleting' "$tmp" | sed 's/^\*deleting *//' > "$PENDING" || true
  rm -f "$tmp"

  # 3. Classify the candidates: moved/renamed vs genuinely deleted.
  classify_deletions

  local n nmoved ndeleted copied
  n=$(wc -l < "$PENDING" | tr -d ' ')
  nmoved=$(wc -l < "$MOVED" | tr -d ' ')
  ndeleted=$(wc -l < "$DELETED" | tr -d ' ')
  copied=$(grep -E 'files transferred' "$log" \
           | grep -oE '[0-9,]+' | tail -1 | tr -d ',' || true)
  copied=${copied:-0}

  if [ "$n" -eq 0 ]; then
    notify "Media mirror OK" \
      "Weekly sync complete. $copied files copied. Nothing queued." \
      low floppy_disk
  elif [ "$ndeleted" -eq 0 ]; then
    notify "Media mirror OK — $nmoved moved" \
"$copied files copied. $nmoved file(s) moved/renamed within fusion
(content preserved); their stale backup paths are queued for cleanup.
No genuine deletions." \
      low floppy_disk
  else
    notify "Media mirror — $ndeleted deletion(s) need review" \
"$copied copied. $ndeleted genuine deletion(s) need review.
$nmoved moved/renamed (content preserved on fusion — safe).
Review:  $DELETED
Approve: sudo media-mirror approve" \
      high "warning,floppy_disk"
  fi
  echo "sync done: $copied copied, $nmoved moved, $ndeleted genuine deletions"
}

cmd_approve() {
  require_root approve
  [ -s "$PENDING" ] || { echo "nothing queued for deletion"; exit 0; }

  # Optional one-time limit override, for large reviewed reconciliation batches.
  local limit="${1:-$MAX_DELETE}"
  case "$limit" in
    "" | *[!0-9]*) die "approve: limit must be a number (got: $limit)" ;;
  esac

  if ! preflight all; then
    notify "Media mirror approve ABORTED — drive offline" \
      "A mergerfs pool member is missing. Nothing was deleted." \
      urgent rotating_light
    die "preflight failed — a pool member drive is offline"
  fi

  local n
  n=$(wc -l < "$PENDING" | tr -d ' ')
  if [ "$n" -gt "$limit" ]; then
    notify "Media mirror approve ABORTED — over limit" \
"$n files are queued for deletion, exceeding the limit of $limit.
Nothing was deleted. A pool drive may be offline, or fusion changed a lot.
If this large batch is expected, re-run: sudo media-mirror approve <limit>" \
      urgent rotating_light
    die "$n deletions exceeds limit=$limit — aborting (override: media-mirror approve <limit>)"
  fi

  local ts dest
  ts=$(date +%Y-%m-%d_%H%M%S)
  dest="$GRAVEYARD/$ts"
  mkdir -p "$dest"

  echo "applying up to $n deletions, graveyard: $dest"
  rsync -aH --delete --backup --backup-dir="$dest" \
    --max-delete="$limit" "${EXCLUDES[@]}" "$SRC/" "$DST/"

  local moved
  moved=$(find "$dest" -type f | wc -l | tr -d ' ')
  mv "$PENDING" "$STATE/approved-$ts.txt"

  notify "Media mirror — $moved files moved to graveyard" \
"Graveyard: $dest
Recover:   sudo media-mirror recover $ts
Auto-pruned after $GRAVEYARD_RETENTION_DAYS days." \
    default wastebasket
  echo "approve done: $moved files moved to $dest"
}

cmd_recover() {
  require_root recover
  local ts="${1:-}"
  if [ -z "$ts" ]; then
    echo "available graveyard snapshots:"
    local snaps
    snaps=$(find "$GRAVEYARD" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
              2>/dev/null | sort || true)
    if [ -n "$snaps" ]; then echo "$snaps" | sed 's/^/  /'; else echo "  (none)"; fi
    echo "usage: sudo media-mirror recover <date>"
    exit 0
  fi
  local src="$GRAVEYARD/$ts"
  [ -d "$src" ] || die "no graveyard snapshot: $ts"

  echo "restoring $src/ -> $DST/"
  rsync -aH "$src/" "$DST/"
  local n
  n=$(find "$src" -type f | wc -l | tr -d ' ')
  notify "Media mirror — recovered $n files" \
    "Restored from graveyard snapshot $ts back into $DST." \
    default white_check_mark
  echo "recovered $n files from $ts"
}

cmd_prune_graveyard() {
  require_root prune-graveyard
  [ -d "$GRAVEYARD" ] || { echo "no graveyard"; exit 0; }
  local pruned=0 d
  while IFS= read -r -d '' d; do
    echo "pruning old graveyard snapshot: $d"
    rm -rf "$d"
    pruned=$((pruned + 1))
  done < <(find "$GRAVEYARD" -mindepth 1 -maxdepth 1 -type d \
             -mtime "+$GRAVEYARD_RETENTION_DAYS" -print0)
  if [ "$pruned" -gt 0 ]; then
    notify "Media mirror — graveyard pruned" \
      "Removed $pruned graveyard snapshot(s) older than $GRAVEYARD_RETENTION_DAYS days." \
      low wastebasket
  fi
  echo "prune done: $pruned snapshot(s) removed"
}

cmd_status() {
  echo "=== media-mirror status ==="
  echo "source:      $SRC"
  echo "destination: $DST"
  echo
  echo "drive preflight:"
  if preflight all; then
    echo "  OK — all pool members online"
  else
    echo "  FAILED — see drives listed above"
  fi
  echo
  if [ -s "$PENDING" ]; then
    echo "queued backup-path removals:"
    [ -f "$MOVED" ]   && echo "  moved/renamed (safe): $(wc -l < "$MOVED" | tr -d ' ')"
    [ -f "$DELETED" ] && echo "  genuine deletions:    $(wc -l < "$DELETED" | tr -d ' ')  (review: $DELETED)"
  else
    echo "queued backup-path removals: none"
  fi
  echo
  echo "graveyard snapshots:"
  local gv=""
  if [ -d "$GRAVEYARD" ]; then
    gv=$(find "$GRAVEYARD" -mindepth 1 -maxdepth 1 -type d 2>/dev/null || true)
  fi
  if [ -n "$gv" ]; then
    du -sh "$GRAVEYARD"/*/ 2>/dev/null | sed 's/^/  /'
  else
    echo "  (none)"
  fi
}

case "${1:-}" in
  sync)            shift; cmd_sync "$@" ;;
  approve)         shift; cmd_approve "$@" ;;
  recover)         shift; cmd_recover "$@" ;;
  prune-graveyard) shift; cmd_prune_graveyard "$@" ;;
  preflight)       shift; cmd_preflight "$@" ;;
  status)          shift; cmd_status "$@" ;;
  *)
    echo "usage: media-mirror {sync|approve [<limit>]|recover [<date>]|prune-graveyard|preflight|status}" >&2
    exit 1 ;;
esac
