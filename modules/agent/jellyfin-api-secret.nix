# Jellyfin API key for the `claude` agent — GROMIT ONLY.
#
# WHY: Jellyfin state has been the stack's last blind spot. /var/lib/jellyfin is
# drwx------ jellyfin:media (owner-only, so the agent's `media` group membership
# does not help) and the existing key lives in the root-only media-curate-env
# secret. Every Jellyfin question this week had to be answered by grepping the
# journal and inferring — which is how "is a library pointed at the manual
# folder?" ended up being answered from LibraryMonitor log lines rather than
# from the library list itself.
#
# Read-only HTTP against localhost:8096. No new shell access, no container
# entry, no extra sudo.
#
# A SEPARATE key from media-curate's on purpose: this one can be revoked alone
# if the agent's access should ever be cut, without breaking the tag-sweep and
# promote jobs that media-curate runs as root.
#
# Usage:
#   set -a; . /run/secrets/jellyfin-api; set +a
#   curl -sS -H "X-Emby-Token: $JELLYFIN_API_KEY" \
#     http://127.0.0.1:8096/Library/VirtualFolders | jq -r '.[].Name'
#
# CHRIS ADDS THE VALUE (the agent has no decryption key — see modules/sops.nix):
#   sops secrets/jellyfin-api.yaml
# with contents:
#   jellyfin-api: |
#     JELLYFIN_API_KEY=<Jellyfin > Dashboard > API Keys > "+">
{ ... }:
{
  sops.secrets."jellyfin-api" = {
    sopsFile = ../../secrets/jellyfin-api.yaml;
    key = "jellyfin-api";
    owner = "claude";
    mode = "0400";
  };
}
