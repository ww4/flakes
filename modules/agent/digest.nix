# Weekly homelab digest — headless Claude Code → published HTML page + a short ntfy.
#
# Runs `claude -p "/catch-up"` non-interactively as the `claude` user, on the
# Claude *subscription* (OAuth creds at ~/.claude/.credentials.json — NOT API
# token-billed; verified 2026-06-13). /catch-up emits the full digest (markdown)
# plus a final `TLDR: …` line.
#
# Output handling (ntfy has no markdown + a tiny body): the full markdown is
# rendered to a styled HTML page at /var/lib/digest/index.html (served at
# digest.rosemaryacres.com — its OWN subdomain, separate origin from the homepage
# PWA so the link isn't captured/404'd by the installed dashboard app), and the
# ntfy notification is just the one-line TLDR + a link to that page.
#
# WorkingDirectory is the docs-repo project dir so the agent's memory loads (a run
# from the wrong dir produced inaccurate results in testing). The explicit
# Environment=PATH below mirrors the claude user's interactive env so /catch-up's
# shell-outs (git/curl/jq/systemctl/gromit-notify) resolve — but note it OVERRIDES
# the systemd `path` option, so cmark-gfm (not in the claude/system profile) is
# referenced by absolute store path in the script instead of via PATH.
{ config, lib, pkgs, ... }:
{
  systemd.services.claude-weekly-digest = {
    description = "Weekly homelab digest (claude -p /catch-up -> page + ntfy)";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    path = [ pkgs.coreutils pkgs.gnugrep pkgs.gnused ];   # cmark-gfm is called by absolute path (see header)
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      StateDirectory = "digest";          # /var/lib/digest (0755, claude-owned; nginx can read)
      WorkingDirectory = "/home/claude/nixos-homelab-improvements";
      TimeoutStartSec = "20min";
      Environment = [
        "HOME=/home/claude"
        "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
        # Marks this as a headless/scheduled run so the agent's Stop reflection
        # hook (claude-harness.nix) no-ops — a digest must never be derailed into
        # doing /retro work. See modules/agent/reflection-hook.sh.
        "CLAUDE_AUTONOMOUS=1"
      ];
    };
    script = ''
      set -uo pipefail
      out=/var/lib/digest
      md="$(timeout 15m claude -p "/catch-up" 2>/dev/null)" \
        || md="# Digest run failed

The weekly digest run did not complete. Check \`journalctl -u claude-weekly-digest\`.

TLDR: Weekly digest run FAILED — check the journal."
      [ -n "$md" ] || md="(empty digest — check journalctl -u claude-weekly-digest)

TLDR: Weekly digest came back empty."

      # Append a "Sentinel activity (last 7 days)" section built from the
      # sentinel incident files (see modules/services/sentinel.nix).
      sentinel_md="$(
        inc=/var/lib/sentinel/incidents
        if [ -d "$inc" ]; then
          cutoff=$(( $(date +%s) - 7*86400 )); total=0; acted=0; lines=""
          for f in "$inc"/*.txt; do
            [ -e "$f" ] || continue
            base="$(basename "$f")"; t="''${base##*-}"; t="''${t%.txt}"
            case "$t" in *[!0-9]*|"") continue;; esac
            [ "$t" -ge "$cutoff" ] || continue
            total=$((total+1)); id="''${base%-*}"
            action="$(grep -m1 '^ACTION:' "$f" 2>/dev/null | sed 's/^ACTION:[[:space:]]*//')"
            case "$action" in ""|[Nn]one) verb="diagnosed";; *) verb="**ACTED**: $action"; acted=$((acted+1));; esac
            lines="$lines- $(date -d "@$t" '+%m-%d %H:%M' 2>/dev/null) \`$id\` — $verb
"
          done
          if [ "$total" -gt 0 ]; then
            printf '## Sentinel activity (last 7 days)\n\n%d incident(s); %d with an action taken. Full log: https://digest.rosemaryacres.com/sentinel/\n\n%s\n' "$total" "$acted" "$lines"
          fi
        fi
      )"
      [ -n "$sentinel_md" ] && md="$md

---

$sentinel_md"

      ts="$(date +%Y-%m-%d)"
      # render markdown -> a styled standalone HTML page
      {
        printf '%s' '<!doctype html><html><head><meta charset="utf-8">'
        printf '%s' '<meta name="viewport" content="width=device-width,initial-scale=1">'
        printf '<title>Gromit homelab digest — %s</title>' "$ts"
        printf '%s' '<style>body{max-width:46rem;margin:2rem auto;padding:0 1rem;font:16px/1.6 system-ui,-apple-system,sans-serif;color:#e6e6e6;background:#181818}h1,h2,h3{line-height:1.25;margin-top:1.4em}code{background:#2c2c2c;padding:.1em .35em;border-radius:3px;font-size:.9em}pre{background:#2c2c2c;padding:.8em;border-radius:6px;overflow:auto}a{color:#6cb6ff}table{border-collapse:collapse;width:100%}td,th{border:1px solid #444;padding:.35em .6em;text-align:left}hr{border:0;border-top:1px solid #444}</style></head><body>'
        printf '%s' "$md" | ${pkgs.cmark-gfm}/bin/cmark-gfm --extension table --extension strikethrough
        printf '<hr><p style="color:#888;font-size:.85em">Generated %s by the weekly digest timer.</p></body></html>' "$(date '+%Y-%m-%d %H:%M %Z')"
      } > "$out/index.html"
      cp -f "$out/index.html" "$out/$ts.html"
      chmod 0644 "$out"/*.html || true

      # one-line TLDR for the notification (fallback to a generic line)
      tldr="$(printf '%s' "$md" | grep -m1 -iE '^TLDR:' | sed -E 's/^[Tt][Ll][Dd][Rr]:[[:space:]]*//')"
      [ -n "$tldr" ] || tldr="Weekly homelab digest is ready."
      gromit-notify "Homelab weekly digest" "$tldr
Full report: https://digest.rosemaryacres.com/" default "calendar"
    '';
  };

  systemd.timers.claude-weekly-digest = {
    description = "Weekly homelab digest";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Mon 08:00";          # America/New_York; Monday-morning rhythm
      Persistent = true;                  # catch up if the box was off
      RandomizedDelaySec = "10m";
    };
  };

  # Serve the rendered digest on its OWN subdomain — a separate origin from the
  # homepage PWA, so the notification link opens as a normal page instead of being
  # captured (and 404'd) by the installed dashboard app's service worker/SPA.
  # DNS: `digest.rosemaryacres.com` -> 100.82.117.116 (proxy off) ALREADY created by
  # the agent via the Cloudflare token (/var/cloudflare-dns-api). Inherits the global
  # source-gate; gets its own ACME cert on deploy.
  services.nginx.virtualHosts."digest.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    root = "/var/lib/digest";
    locations."/".extraConfig = ''
      index index.html;
      autoindex on;
    '';
  };
}
