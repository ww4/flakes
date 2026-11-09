# queue-replies — gromit's half of the agent-vm reply channel (contract v1).
#
# THE PROBLEM. agent-vm files work through the homelab-mcp connector, and until
# now a filed request vanished: nothing told the asking pane that its item had
# been picked up or finished. On 2026-10-12 the connector itself went down and
# agent-vm could not even file, which is what prompted building this properly.
#
# THE SHAPE. Requests carry a header block in the `what` field (`reply-to:`,
# `req:`, optional `answer-note:`). gromit publishes a status to an ntfy topic
# twice — once on pickup (`accepted`), once on close (`answered`/`declined`/
# `failed`). agent-vm streams that topic and types a fixed template into the
# asking pane. Items WITHOUT `reply-to:` behave exactly as before and publish
# nothing, so this is additive.
#
# ⚠️ THE INJECTION BOUNDARY IS THE WHOLE DESIGN. agent-vm types what we publish
# into a live Claude session, so nothing free-text may cross. Three rules hold
# that line, and all three are load-bearing:
#   1. every published field is a fixed word or a regex-matched value
#      (see queue-reply.py) — there is no message field;
#   2. the CONSUMER SCRIPT parses the queue and publishes, never the model, so
#      attacker-influenced text from an untrusted queue item cannot reach the
#      wire;
#   3. human-readable content goes in a NOTE, which agent-vm fetches itself
#      through read_note — a channel it already trusts and controls.
{ config, lib, pkgs, ... }:

let
  topic = "agent-queue-replies";
  replyPort = 8791;          # scoped listener, agent-vm only
  agentVmIp = "100.124.87.61";
  tailnetIp = "100.82.117.116";
  certDomain = "ntfy.rosemaryacres.com";

  queue-reply = pkgs.writers.writePython3Bin "queue-reply" {
    flakeIgnore = [ "E501" "E203" "W503" "W504" ];
  } (builtins.readFile ./queue-reply.py);
in
{
  # ⚠️ GLOBAL nginx recommendedProxySettings OFF — this is an outage fix, not a
  # preference. homelab-modules' nginx-access.nix sets it `lib.mkDefault true`,
  # and the 2026-10-12 12:18 bump (#402) therefore turned it on for every vhost
  # that had not asked for it. It injects a proxy_set_header include into each
  # proxying location, and 18 vhosts here already set `Host` by hand — so nginx
  # sent the header TWICE.
  #
  # A duplicate Host violates RFC 7230 and the upstream answers 400. MEASURED,
  # not assumed, against four different upstreams:
  #   uvicorn (homelab-mcp) 400 · ntfy (Go) 400 · Grafana 400 · Jellyfin 400
  # The MCP connector was down from 12:25 and grafana, jellyfin, recipes,
  # sonarr, radarr, prowlarr, qbittorrent, requests, music, books, prometheus,
  # lidarr, lazylibrarian, abs and pinchflat were all broken the same way.
  #
  # Set here rather than per-vhost because it reverts a global default to the
  # value this box ran on for months. Vhosts that opt in EXPLICITLY are
  # unaffected and keep the include.
  services.nginx.recommendedProxySettings = false;

  environment.systemPackages = [ queue-reply ];

  # ---------------------------------------------------------------- ntfy
  # Explicit, not left to the default. agent-vm cold-starts its stream at
  # `since=12h`, so anything shorter would silently lose replies filed while it
  # was restarting — and a default is not a contract.
  services.ntfy-sh.settings.cache-duration = "12h";

  systemd.tmpfiles.rules = [
    "d /var/lib/queue-reply 0750 claude users -"
  ];

  # ⚠️ NO `--config` FLAG EXISTS on `ntfy user` / `ntfy access`. Passing one
  # makes the unit die right after writing a password file, so the credential
  # exists and the ACCOUNT never does — which presents as "not authorized"
  # while everything looks provisioned. Learned the hard way; see the
  # ntfy-provision unit in homelab-modules for the original bite.
  systemd.services.queue-replies-provision = {
    description = "Provision the ${topic} ntfy ACL (idempotent)";
    wantedBy = [ "multi-user.target" ];
    after = [ "ntfy-sh.service" ];
    requires = [ "ntfy-sh.service" ];
    path = [ pkgs.ntfy-sh pkgs.coreutils pkgs.gnugrep ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    script = ''
      set -euo pipefail
      # ⚠️ TOKENS MUST NOT LIVE UNDER /var/lib/ntfy-sh. That path is a symlink
      # into /var/lib/private, which is `drwx------ root root` — so `claude`
      # cannot traverse to it whatever mode the file has. The publisher runs as
      # claude, so its token goes in our own StateDirectory. Found by testing
      # the publisher as claude and getting EACCES on the path, not the file.
      pubtok=/var/lib/queue-reply/publisher-token.txt
      handoff=/home/claude/secrets-inbox/gromit-replies.env

      # ntfy creates user.db on first start; wait rather than race it. These
      # are server-side commands reading the default /etc/ntfy/server.yml.
      for _ in $(seq 1 30); do
        ntfy user list >/dev/null 2>&1 && break
        sleep 2
      done

      mkpw() {  # mkpw <file>
        if [ ! -s "$1" ]; then
          head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24 > "$1"
          chmod 600 "$1"
        fi
        cat "$1"
      }

      rpw=$(mkpw /var/lib/queue-reply/agent-vm-replies-password.txt)
      ppw=$(mkpw /var/lib/queue-reply/publisher-password.txt)

      # Add, or force the password to match the file, so the file is
      # authoritative and a re-run is self-healing.
      NTFY_PASSWORD="$rpw" ntfy user add agent-vm-replies 2>/dev/null \
        || NTFY_PASSWORD="$rpw" ntfy user change-pass agent-vm-replies
      NTFY_PASSWORD="$ppw" ntfy user add gromit-queue-publisher 2>/dev/null \
        || NTFY_PASSWORD="$ppw" ntfy user change-pass gromit-queue-publisher

      # agent-vm: READ-ONLY on this one topic, nothing else anywhere.
      ntfy access agent-vm-replies '*' deny
      ntfy access agent-vm-replies ${topic} read-only
      # The publisher: write on this topic only.
      ntfy access gromit-queue-publisher '*' deny
      ntfy access gromit-queue-publisher ${topic} write-only

      # ⚠️ THE IMPORTANT LINE. Server-wide `auth-default-access` is
      # `write-only`, i.e. ANONYMOUS MAY PUBLISH to any topic. Without this
      # deny, anyone reaching the server could post a forged reply and agent-vm
      # would type it into a live Claude session. agent-vm raised this and was
      # right: it is a requirement, not belt-and-braces.
      ntfy access everyone ${topic} deny

      if [ ! -s "$pubtok" ]; then
        ntfy token add --label queue-replies gromit-queue-publisher \
          | grep -oE 'tk_[A-Za-z0-9]+' > "$pubtok"
        chown claude:users "$pubtok"; chmod 640 "$pubtok"
      fi

      # The hand-off for agent-vm. Written into Chris's secrets-inbox, 0600,
      # never into a note, a commit or the nix store.
      if [ ! -s "$handoff" ]; then
        rtok=$(ntfy token add --label queue-replies agent-vm-replies \
               | grep -oE 'tk_[A-Za-z0-9]+')
        {
          echo "# gromit -> agent-vm queue reply channel (contract v1)."
          echo "# Install on agent-vm; read-only on ${topic} and nothing else."
          echo "NTFY_BASE=https://${certDomain}:${toString replyPort}"
          echo "NTFY_TOPIC=${topic}"
          echo "NTFY_TOKEN=$rtok"
        } > "$handoff"
        chown claude:users "$handoff"; chmod 600 "$handoff"
      fi
      echo "queue-replies: ACL provisioned; anonymous write to ${topic} DENIED"
    '';
  };

  # ------------------------------------------- the scoped listener: REMOVED
  #
  # ⚠️ THE LISTENER IS DELIBERATELY ABSENT. It was here, on tcp/8791, and it
  # took nginx — and therefore ALL 75 vhosts — down on 2026-10-12 at 15:32:
  #
  #     bind() to 100.82.117.116:8791 failed (98: Address already in use)
  #
  # 8791 is the Yamaha YNC API wrapper's own port. nginx already PROXIES to
  # 127.0.0.1:8791 for net-radio control, so adding a `listen` on it was a
  # direct collision with a service that was there first. The whole design —
  # agreed with agent-vm, reviewed, built and merged — named that port, and
  # nobody ran `ss -ltn | grep 8791` once in three hours.
  #
  # Nothing is lost by leaving it out today: agent-vm has no tailnet grant yet,
  # so the read path was never reachable. The PUBLISH half below works, because
  # it posts to ntfy through the existing 443 vhost.
  #
  # TO RE-ADD IT, in this order:
  #   1. pick a port and PROVE it is free, against BOTH lists:
  #        ss -ltn                                   # what is bound
  #        grep -oE 'proxy_pass http://127.0.0.1:[0-9]+' <nginx.conf>
  #      A proxy TARGET is as taken as a listener. Taken on 2026-10-12:
  #      8787 8789 8790 8791 8799 (and ~60 others — this box is dense).
  #      8793-8798 were free at that time; re-check, do not trust this list.
  #   2. set `onlySSL = true` alongside `ssl = true`, or nginx emits
  #      `listen ... ssl` with no certificate and again refuses to start.
  #   3. run the port-collision and duplicate-header checks before merging.
  #   4. ⚠️ do NOT set `proxy_set_header Host` by hand: with
  #      recommendedProxySettings on, the include sets it too and ntfy (Go)
  #      answers 400 to a duplicate Host — measured, not assumed.
}
