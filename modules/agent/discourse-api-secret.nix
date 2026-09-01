# Discourse API key for the `claude` agent — GROMIT ONLY.
#
# WHY: forum.driveonwood.com is the one production service in Chris's world that
# nothing watches. It sat on Discourse 2026.5.0-latest.1 while upstream moved to
# 2026.9.0-latest — four monthly releases plus the 2026.5.1/5.2 point releases on
# its own line — and nobody noticed, because the only version signal anyone had
# was /about.json read by hand. Backups have the same shape: Discourse takes them
# on its own schedule and reports success only inside the admin UI, so "the
# backup is running" has never been verified from outside that UI.
#
# This key exists so the agent can answer both questions on a timer instead of on
# request: is the forum behind upstream, and did last night's backup actually
# finish. Read-only HTTP against the public host — no shell on the droplet, no
# DigitalOcean credentials, nothing that can change forum state.
#
# The value is a REMOTE credential (not localhost like arr/jellyfin), so treat a
# leak as more serious than the other agent keys: it authenticates against a
# public production forum with ~12 years of irreplaceable content. It is
# claude-owned 0400 for that reason, and it is deliberately a SEPARATE key from
# anything the forum itself uses, so revoking it cannot break the site.
#
# Usage:
#   set -a; . /run/secrets/discourse-api; set +a
#   curl -sS -H "Api-Key: $DISCOURSE_API_KEY" \
#        -H "Api-Username: $DISCOURSE_API_USERNAME" \
#        https://forum.driveonwood.com/admin/dashboard.json \
#     | jq '.version_check'
#
# CHRIS ADDS THE VALUE (the agent has no decryption key — see modules/sops.nix).
# The encrypted file already exists with a PLACEHOLDER, so this is an edit, not a
# create:
#
#   sops secrets/discourse-api.yaml
#
# and replace PLACEHOLDER on the DISCOURSE_API_KEY line. Leave the shape alone:
#   discourse-api: |
#     DISCOURSE_API_KEY=<the key>
#     DISCOURSE_API_USERNAME=<the username the key acts as>
#
# WHERE THE KEY COMES FROM — Discourse admin, not the droplet:
#   forum.driveonwood.com/admin/api/keys → "+ New API Key"
#     Description : homelab agent — read-only monitoring (gromit)
#     User Level  : Single User → an ADMIN account
#
# On the acting account: prefer a dedicated Discourse admin user (e.g.
# `homelab-agent`) over Chris's own login. Same access either way, but it keeps
# the staff action log attributable — every API action shows up as that user —
# and it can be suspended without touching Chris's account. Binding to his own
# account works and is one less step; the tradeoff is only auditability.
#
# On scopes: the two endpoints this needs (/admin/dashboard.json for the version
# check, /admin/backups.json for backup freshness) are ADMIN routes. Discourse's
# granular-scope list is oriented at content resources (topics, posts, users,
# categories …) and — as far as the agent can tell without a key in hand — does
# not carry a scope that covers admin backups. So: create it as a GLOBAL key
# bound to a single admin user. The agent will confirm empirically on first use
# which routes actually answer, and if a narrower granular key turns out to
# cover both, this comment gets corrected and the key gets reissued smaller.
# Do NOT create an "All Users" key — that one can impersonate any account and is
# far more access than monitoring needs.
{ ... }:
{
  sops.secrets."discourse-api" = {
    sopsFile = ../../secrets/discourse-api.yaml;
    key = "discourse-api";
    owner = "claude";
    mode = "0400";
  };
}
