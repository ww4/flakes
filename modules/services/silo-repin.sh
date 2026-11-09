#!/usr/bin/env bash
# Resolve current image digests + provenance for silo.nix.
#
# WHY: Silo publishes no tagged releases — only ~100 commit-SHA tags and
# `latest` — so a digest is the only stable handle, and a digest on its own
# tells you nothing about WHAT you pinned. This prints the digest together with
# the upstream commit, build number and build time baked into the image's OCI
# labels, so the pin in silo.nix stays auditable and a later re-test can say
# exactly what moved.
#
# Usage:  ./silo-repin.sh            # print the nix snippet + provenance
#         ./silo-repin.sh --check    # exit 1 if silo.nix is behind upstream
#
# Needs: curl, python3, and network. Reads nothing secret.
set -euo pipefail

GHCR_REPO="silo-server/silo-server"
HUB_PG="pgvector/pgvector"; HUB_PG_TAG="pg18"
HUB_REDIS="library/redis"; HUB_REDIS_TAG="alpine"

ACCEPT=(
  -H "Accept: application/vnd.oci.image.index.v1+json"
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json"
  -H "Accept: application/vnd.oci.image.manifest.v1+json"
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json"
)

die() { echo "silo-repin: $*" >&2; exit 1; }

ghcr_token() {
  curl -fsS "https://ghcr.io/token?scope=repository:$1:pull&service=ghcr.io" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'
}
hub_token() {
  curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$1:pull" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'
}

# digest <registry-host> <token> <repo> <ref>
digest() {
  local host=$1 tok=$2 repo=$3 ref=$4 out
  out=$(curl -fsSI -H "Authorization: Bearer $tok" "${ACCEPT[@]}" \
        "https://$host/v2/$repo/manifests/$ref" \
        | tr -d '\r' | awk 'tolower($1)=="docker-content-digest:"{print $2}')
  # ⚠️ An empty digest here means the lookup FAILED, not that there is no
  # image. Fail loudly rather than writing an empty pin into silo.nix.
  [ -n "$out" ] || die "could not resolve digest for $repo:$ref (lookup failed)"
  echo "$out"
}

# provenance <token> <repo> <ref> -> prints "commit|version|created"
provenance() {
  local tok=$1 repo=$2 ref=$3 idx amd cfg
  idx=$(curl -fsS -H "Authorization: Bearer $tok" "${ACCEPT[@]}" \
        "https://ghcr.io/v2/$repo/manifests/$ref")
  # Multi-arch index -> pick linux/amd64; a single manifest has no .manifests.
  amd=$(echo "$idx" | python3 -c '
import sys,json
d=json.load(sys.stdin)
ms=d.get("manifests")
if not ms: print(""); raise SystemExit
for m in ms:
    p=m.get("platform",{})
    if p.get("os")=="linux" and p.get("architecture")=="amd64":
        print(m["digest"]); raise SystemExit
print("")')
  if [ -n "$amd" ]; then
    cfg=$(curl -fsS -H "Authorization: Bearer $tok" "${ACCEPT[@]}" \
          "https://ghcr.io/v2/$repo/manifests/$amd" \
          | python3 -c 'import sys,json;print(json.load(sys.stdin)["config"]["digest"])')
  else
    cfg=$(echo "$idx" | python3 -c 'import sys,json;print(json.load(sys.stdin)["config"]["digest"])')
  fi
  curl -fsSL -H "Authorization: Bearer $tok" "https://ghcr.io/v2/$repo/blobs/$cfg" \
    | python3 -c '
import sys,json
d=json.load(sys.stdin)
l=(d.get("config") or {}).get("Labels") or {}
print("|".join([l.get("org.opencontainers.image.revision","?"),
                l.get("org.opencontainers.image.version","?"),
                l.get("org.opencontainers.image.created","?")]))'
}

GT=$(ghcr_token "$GHCR_REPO")
SILO_D=$(digest ghcr.io "$GT" "$GHCR_REPO" latest)
IFS='|' read -r REV VER CREATED <<<"$(provenance "$GT" "$GHCR_REPO" latest)"

PT=$(hub_token "$HUB_PG"); PG_D=$(digest registry-1.docker.io "$PT" "$HUB_PG" "$HUB_PG_TAG")
RT=$(hub_token "$HUB_REDIS"); RD_D=$(digest registry-1.docker.io "$RT" "$HUB_REDIS" "$HUB_REDIS_TAG")

if [ "${1:-}" = "--check" ]; then
  here=$(dirname "$0")
  if grep -q "$SILO_D" "$here/silo.nix"; then
    echo "silo.nix is pinned to current upstream latest ($REV, $VER)"
    exit 0
  fi
  echo "silo.nix is BEHIND upstream latest:"
  echo "  upstream: $SILO_D  ($REV, $VER, built $CREATED)"
  echo "  run $0 and paste the snippet below into silo.nix"
  exit 1
fi

cat <<OUT
# Resolved $(date -u +%Y-%m-%dT%H:%M:%SZ) by silo-repin.sh — do not hand-edit.
#
# silo-server provenance:
#   upstream commit : $REV
#   image version   : $VER
#   image built     : $CREATED
#   source          : https://github.com/Silo-Server/silo-server/commit/$REV

    image = "ghcr.io/silo-server/silo-server:latest@$SILO_D";
    postgresImage = "pgvector/pgvector:pg18@$PG_D";
    redisImage = "redis:alpine@$RD_D";
OUT
