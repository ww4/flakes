# DigitalOcean read-only credentials for the `claude` agent — GROMIT ONLY.
#
# Two credentials, two different APIs, deliberately in one env-shaped secret:
#
#   DO_SPACES_KEY_ID / DO_SPACES_SECRET  — S3 API at nyc3.digitaloceanspaces.com
#   DO_API_TOKEN                         — REST API at api.digitalocean.com
#
# WHY: the DOW forum's uploads and its database backups both live in DO Spaces,
# and nothing on gromit could see either. The question that prompted this was
# "are there lifecycle rules on the uploads bucket, and do they protect the
# images or delete them?"
#
# ── VERIFIED SCOPE BOUNDARY (measured 2026-09-02, not assumed) ─────────────
#
# The Spaces key is correctly scoped and read-only. What it CAN do:
#   ✅ s3api list-objects-v2      on `dow` and `dow-discourse-backups`
#   ✅ s3api get-bucket-location  → nyc3
#   ✅ s3api get-bucket-versioning → exit 0, EMPTY body
#
# What it CANNOT do — the read-only scope excludes bucket CONFIGURATION:
#   ❌ get-bucket-lifecycle-configuration   AccessDenied
#   ❌ get-bucket-acl / -cors / -policy      AccessDenied
#   ❌ list-buckets (account-level)          AccessDenied
#
# ⚠️ So the lifecycle rules still cannot be read directly. This was flagged as
# uncertain before the key was created and it turned out to be the limiting
# case; do NOT widen the key on that account without Chris deciding it is worth
# the blast radius. The operational question was answerable anyway — see below.
#
# ⚠️ AND THE DO API TOKEN IS NOT A WAY AROUND IT. Verified by probe, not
# assumed: /v2/spaces/buckets and /v2/spaces both 404. The REST API exposes
# Spaces *keys* (/v2/spaces/keys) and CDN endpoints, but no bucket
# configuration whatsoever. Bucket config is S3-API-only.
#
# ── WHAT THE EVIDENCE SAYS ABOUT THE IMAGES ────────────────────────────────
#
# `get-bucket-versioning` returns exit 0 with an empty body on both buckets.
# Permitted call, no Status field → versioning has never been enabled (and DO
# Spaces may not support it at all). Either way: NO VERSION HISTORY. Nothing
# would survive an overwrite or delete.
#
# A lifecycle rule almost certainly DOES exist on `dow`, and it is a DELETION
# rule, inferred rather than read: the `tombstone/` prefix holds only 7 objects,
# oldest 2026-08-08 — about 25 days. Discourse's tombstone retention default is
# 30. With no expiry rule, tombstoned objects would accumulate for years, not
# weeks. So the rule Chris remembered is real, and it is the one that makes a
# deletion permanent — not a safety net.
#
# Usage:
#   set -a; . /run/secrets/digitalocean; set +a
#   AWS_ACCESS_KEY_ID=$DO_SPACES_KEY_ID AWS_SECRET_ACCESS_KEY=$DO_SPACES_SECRET \
#   AWS_DEFAULT_REGION=$DO_SPACES_REGION AWS_EC2_METADATA_DISABLED=true \
#     nix run nixpkgs#awscli2 -- --endpoint-url "$DO_SPACES_ENDPOINT" \
#       s3api list-objects-v2 --bucket dow --max-items 5
#
#   curl -H "Authorization: Bearer $DO_API_TOKEN" https://api.digitalocean.com/v2/droplets
#
# No S3 tooling is installed on gromit and none needs to be — awscli2, s3cmd,
# rclone and doctl are all reachable via `nix run nixpkgs#<tool> --`.
#
# ROTATION: via `~/secrets-inbox/digitalocean.env` — Chris drops new values in,
# the agent verifies, encrypts, PRs and deletes the cleartext. No sops or age
# key needed on his side.
{ ... }:
{
  sops.secrets."digitalocean" = {
    sopsFile = ../../secrets/digitalocean.yaml;
    key = "digitalocean";
    owner = "claude";
    mode = "0400";
  };
}
