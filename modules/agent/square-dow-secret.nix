# Square API token for the DOW account — claude agent, GROMIT ONLY.
#
# WHY: the member reconciliation and, later, dow-orders' Stripe-era coexistence
# need to read Square: Customers, Orders (full history 2013→), Payments,
# Catalog. Verified on first use 2026-09-02 against /v2/locations
# (location 7WZQD4K0PX3WE, "Drive On Wood").
#
# ⚠️ FULL-ACCESS BY SQUARE'S DESIGN. Square dashboard tokens cannot be scoped
# read-only (that exists only in their OAuth flow for third-party apps). Chris
# accepted this; usage is read-only BY PRACTICE — list/get calls only. Kill
# switch: regenerate the token in the Square developer dashboard
# (app: dow-member-reconciliation).
#
# Usage:
#   set -a; . /run/secrets/square-dow; set +a
#   curl -H "Authorization: Bearer $SQUARE_DOW_ACCESS_TOKEN" \
#        -H "Square-Version: 2025-01-23" https://connect.squareup.com/v2/locations
#
# ROTATION: via ~/secrets-inbox/ (template flow), one PR, agent verifies first.
{ ... }:
{
  sops.secrets."square-dow" = {
    sopsFile = ../../secrets/square-dow.yaml;
    key = "square-dow";
    owner = "claude";
    mode = "0400";
  };
}
