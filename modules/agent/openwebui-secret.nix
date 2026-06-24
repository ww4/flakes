# Open WebUI API key for the `claude` agent — GROMIT ONLY.
#
# The agent uses this to drive Open WebUI's API (RAG knowledge-base ingestion +
# automation). Agent-only, so claude-readable at /run/secrets/openwebui-api.
# Populated via a staging file then migrated here (see the
# secrets-handling-preference memory; same model as cloudflare-dns-api).
#
# Split out of modules/agent/claude-user.nix so that shared module carries no
# sops dependency and can be reused on hosts whose SSH host key isn't a sops
# recipient (e.g. marcus). Decryption model: see modules/sops.nix + ./.sops.yaml.
{ ... }:
{
  sops.secrets."openwebui-api" = {
    sopsFile = ../../secrets/openwebui-api.yaml;
    key = "openwebui-api";
    owner = "claude";
    mode = "0400";
  };
}
