# Cloudflare API token for the lock3.net zone — GROMIT ONLY, agent-only.
#
# WHY A SECOND CLOUDFLARE TOKEN: the existing `cloudflare-dns-api` secret is
# scoped to rosemaryacres.com and cannot see lock3.net at all (verified by
# listing zones — it returns exactly one, and it is not this one). Chris's
# business site is a separate zone, so it gets a separate least-privilege
# token rather than widening the homelab one.
#
# SCOPE (zone lock3.net only, four permissions):
#   Zone -> Zone           Read    find the zone id
#   Zone -> DNS            Edit    records: Postmark DKIM, future changes
#   Zone -> Zone Settings  Edit    SSL mode
#   Zone -> Zone WAF       Edit    the contact-form rules
#
# WHAT IT IS FOR: the agent manages lock3.net's DNS and WAF directly instead of
# handing Chris a list of dashboard clicks. Already used to create the
# contact-form rate-limit rule and the non-US managed-challenge rule after the
# site took its first form spam on 2026-09-01.
#
# NOT reachable with this token, so do not go looking: Bot Fight Mode
# (/bot_management returns an authentication error on a free zone; it is a
# dashboard-only toggle under Security -> Bots).
#
# Env-shaped so scripts can source it:
#   set -a; . /run/secrets/cloudflare-lock3-api; set +a
#   curl -sS -H "Authorization: Bearer $CLOUDFLARE_LOCK3_API_TOKEN" \
#     https://api.cloudflare.com/client/v4/zones
#
# The value is already encrypted in secrets/cloudflare-lock3-api.yaml — unlike
# arr-api and friends, Chris does not need to add it. It is the ROTATED token:
# the first one was exposed in a session transcript by a file-change
# notification quoting the staging file, and was revoked on 2026-09-01
# (confirmed dead: "Invalid access token"). To rotate again, mint a replacement
# in the Cloudflare dashboard and `sops secrets/cloudflare-lock3-api.yaml`.
{ ... }:
{
  sops.secrets."cloudflare-lock3-api" = {
    sopsFile = ../../secrets/cloudflare-lock3-api.yaml;
    key = "cloudflare-lock3-api";
    owner = "claude";
    mode = "0400";
  };
}
