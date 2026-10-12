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

  # ------------------------------------------------- the scoped listener
  # NOT port 443. gromit's 443 is the shared nginx for every vhost and its
  # allow-list admits the whole tailnet CGNAT range, so opening 443 to agent-vm
  # would hand it the IP gate on every site here. This is the jump-host pattern
  # already proven for homelab-mcp: one port, one peer, one path.
  services.nginx.virtualHosts."${certDomain}-queue-replies" = {
    serverName = certDomain;
    listen = [{ addr = tailnetIp; port = replyPort; ssl = true; }];
    # Reuses the existing ntfy certificate so agent-vm connects BY NAME and
    # validates normally — no self-signed trust to distribute.
    useACMEHost = certDomain;
    locations = {
      # Exactly one path, and only GET. `=` is an exact match, so nothing else
      # on the ntfy API is reachable through this port.
      "= /${topic}/json" = {
        proxyPass = "http://127.0.0.1:8090";
        extraConfig = ''
          limit_except GET { deny all; }
          allow ${agentVmIp};
          deny all;
          # The stream is long-lived SSE-style; buffering would hold replies
          # until a buffer filled, which presents as a channel that hangs.
          proxy_buffering off;
          proxy_read_timeout 1h;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
        '';
      };
      # 404 everything else, as agreed.
      "/" = {
        extraConfig = ''
          allow ${agentVmIp};
          deny all;
          return 404;
        '';
      };
    };
  };

  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ replyPort ];
}
