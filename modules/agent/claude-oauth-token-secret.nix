# A one-year subscription OAuth token for the UNATTENDED `claude -p` units.
#
# Minted with `claude setup-token` on 2026-10-05, so it lapses 2027-10-05.
# ⚠️ Nothing on the box can read that expiry out of the token — unlike the
# interactive credential, which carries its own `refreshTokenExpiresAt`. The date
# above and in `claude-auth`'s state file are the only record there is.
#
# WHY: the scheduled runs authenticate with /home/claude/.claude/.credentials.json,
# whose refresh token is a ~29-day wall; when it lapses every scheduled job fails
# at once and only an interactive login fixes it. Chris: "Its annoying to find out
# I'm logged out because a bunch of auto-run commands start failing."
#
# ⚠️ SCOPED TO UNITS ON PURPOSE — DO NOT PUT THIS IN A PROFILE OR GLOBAL env.
# Per code.claude.com/docs/en/authentication the token "can only make model
# requests, so it can't establish Remote Control sessions or fetch claude.ai
# connectors", and CLAUDE_CODE_OAUTH_TOKEN outranks the /login credential
# (precedence 5 vs 7) in every session that reads it. Exported globally it would
# silently break Remote Control on all 16 herdr panes — which is how Chris sees
# them in the Claude app — and break the Homelab MCP connector. `claude-run`
# sources it per invocation; nothing else should.
{ ... }:
{
  sops.secrets."claude-oauth-token" = {
    sopsFile = ../../secrets/claude-oauth-token.yaml;
    key = "claude-oauth-token";
    owner = "claude";
    mode = "0400";
  };
}
