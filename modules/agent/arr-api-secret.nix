# *arr API keys for the `claude` agent — GROMIT ONLY.
#
# WHY: the agent can read container logs but cannot query the *arr apps, so
# questions like "which indexer served this grab, and under which quality
# profile?" have repeatedly dead-ended in "check the UI yourself". `docker exec`
# is deliberately NOT on the agent's sudo allowlist (it dumps container env,
# including the gluetun WireGuard key), so an API key is the least-privilege way
# in: read-only HTTP against localhost, no new shell access, no container entry.
#
# Agent-only, so claude-readable at /run/secrets/arr-api. Same model as
# openwebui-api and cloudflare-dns-api.
#
# The file is env-shaped so scripts can source it:
#   set -a; . /run/secrets/arr-api; set +a
#   curl -sS -H "X-Api-Key: $SONARR_API_KEY" http://127.0.0.1:8989/api/v3/series
#
# CHRIS ADDS THE VALUE (the agent has no decryption key — see modules/sops.nix):
#   sops secrets/arr-api.yaml
# then make the contents:
#   arr-api: |
#     SONARR_API_KEY=<Sonarr   > Settings > General > API Key>
#     RADARR_API_KEY=<Radarr   > Settings > General > API Key>
#     PROWLARR_API_KEY=<Prowlarr > Settings > General > API Key>
# The Sonarr and Radarr values are the same two already in
# /var/lib/recyclarr/secrets.yml, so they can be copied from there.
{ ... }:
{
  sops.secrets."arr-api" = {
    sopsFile = ../../secrets/arr-api.yaml;
    key = "arr-api";
    owner = "claude";
    mode = "0400";
  };
}
