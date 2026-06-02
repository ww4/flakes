#!/usr/bin/env bash
# sync-dashboards.sh — git-backed Grafana dashboards (deliberately NOT Nix-provisioned).
#
# Grafana 13's dashboard provisioning breaks anonymous iframe embeds (its
# apiserver won't grant the anon Viewer read access to provisioned dashboards,
# and its resource manager wedges them in the General folder). So we keep
# dashboards imperative and snapshot them to git instead:
#
#   sudo ./sync-dashboards.sh backup    # export live dashboards -> ./dashboards/<uid>.json
#   sudo ./sync-dashboards.sh restore   # recreate them in Grafana from those files (DR)
#
# After `backup`, commit ./dashboards/*.json. `restore` recreates each in its
# original folder, granting Viewer:View so the Homepage iframes load anonymously.
# (General-folder dashboards are restored as-is; anon can't embed those — keep
# embeddable dashboards in a real folder like "Temperatures".)
#
# Needs root (reads the Grafana admin password). jq is pulled via nix if absent.
set -euo pipefail
command -v jq >/dev/null 2>&1 || exec nix shell --extra-experimental-features 'nix-command flakes' nixpkgs#jq --command bash "$0" "$@"

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/dashboards"
G="${GRAFANA_URL:-http://127.0.0.1:3001}"
AUTH="admin:$(cat "${GRAFANA_PW_FILE:-/var/lib/grafana/admin_password}")"
api() { curl -fsS -u "$AUTH" "$@"; }

backup() {
  mkdir -p "$DIR"
  local n=0 uid
  while read -r uid; do
    [ -n "$uid" ] || continue
    local j folder
    j=$(api "$G/api/dashboards/uid/$uid")
    folder=$(jq -r '.meta.folderTitle // "General"' <<<"$j")
    jq --arg f "$folder" '{folderTitle:$f, dashboard:(.dashboard + {id:null})}' <<<"$j" > "$DIR/$uid.json"
    echo "  backed up: $uid  (folder: $folder)"; n=$((n+1))
  done < <(api "$G/api/search?type=dash-db" | jq -r '.[].uid')
  echo "done: $n dashboard(s) -> $DIR  (now: git add + commit)"
}

ensure_folder() {  # title -> stdout folderUid ("" = General); grants Viewer:View on real folders
  local title="$1" fuid=""
  case "$title" in General|Dashboards|"") echo ""; return ;; esac
  fuid=$(api "$G/api/search?type=dash-folder" | jq -r --arg t "$title" '.[]|select(.title==$t)|.uid' | head -1)
  if [ -z "$fuid" ]; then
    fuid=$(api -X POST "$G/api/folders" -H 'Content-Type: application/json' \
           -d "$(jq -n --arg t "$title" '{title:$t}')" | jq -r '.uid')
  fi
  api -X POST "$G/api/folders/$fuid/permissions" -H 'Content-Type: application/json' \
      -d '{"items":[{"role":"Viewer","permission":1},{"role":"Editor","permission":2}]}' >/dev/null || true
  echo "$fuid"
}

restore() {
  shopt -s nullglob
  local n=0 ok=0 f
  for f in "$DIR"/*.json; do
    local uid title fuid resp
    uid=$(basename "$f" .json)
    title=$(jq -r '.folderTitle // "General"' "$f")
    fuid=$(ensure_folder "$title")
    # plain curl (no -f) so we can inspect the body rather than just abort
    resp=$(curl -sS -u "$AUTH" -X POST "$G/api/dashboards/db" -H 'Content-Type: application/json' \
           -d "$(jq -c --arg fu "$fuid" '{dashboard:.dashboard, folderUid:$fu, overwrite:true}' "$f")" 2>&1 || true)
    n=$((n+1))
    if grep -q '"status":"success"' <<<"$resp"; then
      echo "  restored: $uid -> '$title'"; ok=$((ok+1))
    elif grep -qi 'provisioned' <<<"$resp"; then
      echo "  skipped:  $uid (already present, locked as provisioned — fine; DR on a clean DB creates it fresh)"
    else
      echo "  FAILED:   $uid -> $(head -c 140 <<<"$resp")"
    fi
  done
  echo "done: $ok restored, $((n-ok)) skipped/failed (of $n)"
}

case "${1:-}" in
  backup)  backup ;;
  restore) restore ;;
  *) echo "usage: sudo $0 {backup|restore}" >&2; exit 1 ;;
esac
