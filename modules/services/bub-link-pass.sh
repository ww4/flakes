#!/usr/bin/env bash
# bub-link-pass — hardlink Rick's files that gromit already has, CO-LOCATED on
# the master's branch (direct on-branch ln → no mergerfs, no EXDEV, ever).
# Never copies. Anything that can't link is counted and left for the copy pass.
#
#   DRYRUN=1 ./bub-link-pass.sh [SUBTREE]   # report only (default)
#   DRYRUN=0 ./bub-link-pass.sh [SUBTREE]   # actually link
#
# SUBTREE (optional, relative to /mnt/fusion) scopes the run, e.g. "ISOs".
set -uo pipefail

DRYRUN=${DRYRUN:-1}
SUBTREE=${1:-.}
POOL=/mnt/backup/all
BRANCHES=(D1 D2 D3 D4)
BUB='ssh -i /root/.ssh/id_ed25519 -p 4089 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 chris@100.112.10.93'
INV=/tmp/rick_inv.txt

# top-level paths to skip (mirror bub-mirror's excludes)
skip_rel() {
  case "$1" in
    pinchflat/*|arr/*|restic/*|.graveyard/*|shows/*|recup_dir*|\$RECYCLE.BIN/*|System\ Volume\ Information/*|.Trash-1000/*) return 0 ;;
    *) return 1 ;;
  esac
}

echo "== fetching Rick's inventory ($SUBTREE) =="
$BUB "cd /mnt/fusion && find '$SUBTREE' -type f -printf '%s\t%p\n'" > "$INV" 2>/dev/null
echo "   $(wc -l < "$INV") files in inventory"
echo "== mode: $([ "$DRYRUN" = 0 ] && echo 'LIVE (linking)' || echo 'DRY-RUN (report only)') =="

linked=0 relinked=0 already=0 unique=0 diffsize=0 nomaster=0 skipped=0
while IFS=$'\t' read -r size rel; do
  [ -n "${rel:-}" ] || continue
  rel=${rel#./}                                   # normalize "./Movies/x" -> "Movies/x" so excludes match
  if skip_rel "$rel"; then skipped=$((skipped+1)); continue; fi
  master="$POOL/$rel"
  if [ ! -f "$master" ]; then unique=$((unique+1)); continue; fi          # gromit lacks it -> copy pass
  msize=$(stat -c %s "$master" 2>/dev/null || echo -1)
  if [ "$msize" != "$size" ]; then diffsize=$((diffsize+1)); continue; fi # different version -> copy pass
  # locate master's physical branch
  mb=""; for D in "${BRANCHES[@]}"; do if [ -e "/mnt/backup/$D/$rel" ]; then mb=$D; break; fi; done
  if [ -z "$mb" ]; then nomaster=$((nomaster+1)); continue; fi
  ro="$POOL/rick-offsite/$rel"
  if [ -e "$ro" ]; then
    if [ "$(stat -c %i "$ro" 2>/dev/null)" = "$(stat -c %i "$master" 2>/dev/null)" ]; then
      already=$((already+1)); continue                                    # already correctly hardlinked
    fi
    # stray / wrong-branch copy: drop every instance, then relink co-located
    if [ "$DRYRUN" = 0 ]; then for D in "${BRANCHES[@]}"; do rm -f "/mnt/backup/$D/rick-offsite/$rel"; done; fi
    relinked=$((relinked+1))
  else
    linked=$((linked+1))
  fi
  if [ "$DRYRUN" = 0 ]; then
    mkdir -p "/mnt/backup/$mb/rick-offsite/$(dirname "$rel")"
    ln -f "/mnt/backup/$mb/$rel" "/mnt/backup/$mb/rick-offsite/$rel" || nomaster=$((nomaster+1))
  fi
done < "$INV"

echo "== results =="
printf "  new hardlinks:        %d\n" "$linked"
printf "  re-linked (was copy): %d   <- reclaims wasted space\n" "$relinked"
printf "  already hardlinked:   %d\n" "$already"
printf "  Rick-unique:          %d   <- copy pass handles these\n" "$unique"
printf "  different size:       %d   <- copy pass (different version)\n" "$diffsize"
printf "  skipped (excluded):   %d\n" "$skipped"
printf "  master vanished:      %d\n" "$nomaster"
