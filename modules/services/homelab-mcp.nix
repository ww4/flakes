# homelab-mcp — a Model Context Protocol server giving Claude in the app
# (phone, web, desktop) a seat at the SilverBullet space.
#
# PHASE C: published to the public internet via Tailscale Funnel.
#
# It has to be public. Anthropic's cloud connects to the connector outbound from
# 160.79.104.0/21 — it is NOT the phone or the browser that connects, even for
# Desktop and Cowork, because remote connectors are brokered through the account:
#   "Claude connects to your remote MCP server from Anthropic's cloud
#    infrastructure, rather than from your local device."
#   "Servers hosted on a private corporate network, behind a VPN, or blocked by
#    a firewall won't connect."
# So a tailnet-only address can never serve this, and gromit has no existing
# public ingress to reuse — the rosemaryacres.com names resolve *publicly* to
# 100.82.117.116, this box's Tailscale CGNAT address (see nginx-access.nix).
#
# Funnel rather than the originally-planned Cloudflare tunnel, for two reasons
# beyond it being fewer moving parts: TLS terminates HERE (Cloudflare would
# terminate at their edge and see note plaintext), and it needs no port-forward,
# no DNS record and no WAF rule. What it costs is that *.ts.net names are
# published to public Certificate Transparency logs, so the hostname is
# enumerable and cannot be treated as a secret — hence the two gates below.
#
# THE TWO GATES, in order:
#   1. Source IP — nginx allows only 160.79.104.0/21. This is the equivalent of
#      the Cloudflare WAF rule in the original plan, and it is the reason Funnel
#      runs in TLS-terminated-TCP mode with PROXY protocol rather than plain
#      --https: --https gives the backend no way to see the real client address.
#   2. An unguessable 128-bit path prefix, generated on the box (see below).
#
# The chain is:
#   internet :8443 → tailscaled (terminates TLS, prepends PROXY v2)
#                  → nginx 127.0.0.1:8788 (real-IP + allowlist)
#                  → homelab-mcp 127.0.0.1:8787
#
# Endpoint: https://gromit.<tailnet>.ts.net:8443/<prefix>/mcp
#
# ⚠️ THE FUNNEL PORT MUST NOT BE 443. Funnel offers 443/8443/10000, 443 is the
# obvious pick, and it took nginx down across the whole box on first deploy.
# `tailscale funnel --tls-terminated-tcp=<port>` makes tailscaled BIND
# <tailnet-ip>:<port>; nginx binds the wildcard 0.0.0.0:443. A wildcard bind and
# a specific-address bind of the same port collide, so nginx died with
#   [emerg] bind() to 0.0.0.0:443 failed (98: Address already in use)
# and, having Restart=always, sat in a restart loop taking EVERY vhost with it —
# Forgejo included, which is the route config changes reach this box by.
# Nothing about the symptom points here: the funnel reports healthy, and it is
# nginx that looks broken.
#
# Why it exists: a chat in the Claude app has no filesystem and no route into
# this network, so an insight from a phone conversation never reaches the
# knowledgebase, and the conversation itself has no idea what the homelab is.
# A remote MCP connector is the only door Anthropic's product offers.
#
# Writes go DIRECTLY to the space filesystem, not through SilverBullet's HTTP
# API. That API is authless here and also exposes POST /.shell (arbitrary
# command execution as the silverbullet user), so anything holding a credential
# for it holds RCE. This service never talks to it. The claude user already has
# recursive POSIX ACLs on the space — see modules/services/silverbullet.nix.
{ config, lib, pkgs, ... }:

let
  package = pkgs.callPackage ../../pkgs/homelab-mcp { };

  port = 8787;        # the MCP server itself — loopback, never published
  proxyPort = 8788;   # nginx's PROXY-protocol listener, fed only by tailscaled

  # The public Funnel port. 8443, NOT 443 — see the collision note in the header.
  # Funnel permits 443/8443/10000 only, and 443 is nginx's.
  funnelPort = 8443;
  spaceDir = "/var/lib/silverbullet";

  stateDir = "/var/lib/homelab-mcp";
  prefixEnv = "${stateDir}/path-prefix.env";

  # The Funnel hostname. Not a secret — Tailscale publishes every *.ts.net name
  # it issues a certificate for to the public Certificate Transparency logs, so
  # this is discoverable whatever we do. Treat it as a public address and let the
  # two gates do the work.
  #
  # The server has to be TOLD this. The MCP SDK auto-enables DNS-rebinding
  # protection whenever the bind address is loopback and then only accepts
  # 127.0.0.1/localhost Host headers, so without it every proxied request comes
  # back 421 Misdirected Request. See build_transport_security() in server.py.
  publicHost = "gromit.sheep-trout.ts.net";

  # Anthropic's published OUTBOUND range — the addresses their cloud makes MCP
  # tool calls from. Deliberately not the 2607:6bc0::/48 on the same docs page:
  # that one is INBOUND (where Anthropic receives connections) and allowing it
  # here would widen the gate for no reason.
  anthropicEgress = "160.79.104.0/21";
in
{
  # --- the path prefix ---------------------------------------------------
  # Generated on the box rather than carried in sops. It is a rotatable URL
  # token, not a shared credential: nothing else needs to know it, it never has
  # to survive a restore, and rotating it is `rm` + restart. Keeping it out of
  # the repo also means the endpoint URL is not recoverable from git history.
  #
  # To read it: sudo cat /var/lib/homelab-mcp/path-prefix.env
  # To rotate:  sudo rm /var/lib/homelab-mcp/path-prefix.env && sudo systemctl restart homelab-mcp
  systemd.services.homelab-mcp-path-prefix = {
    description = "Generate the homelab-mcp secret path prefix if absent";
    wantedBy = [ "multi-user.target" ];
    before = [ "homelab-mcp.service" ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };

    # Written to a temp file and moved into place, so ${prefixEnv} only ever
    # exists fully-written and correctly-permissioned.
    #
    # The first version chmod'd in place after the redirect, and a failure
    # between the two left a 0600 file that the NEXT run skipped, because the
    # guard is `! -s` — non-empty means "already done". So the unit failed once,
    # succeeded on retry, and the retry's success hid the fault. (What failed was
    # `chown root:claude`: there is no `claude` GROUP on this box — that user's
    # primary group is `users`. `set -e` then aborted before the chmod.)
    #
    # Root-only by design. systemd reads EnvironmentFile as PID 1, so the service
    # gets it while running as claude, and nothing else needs it. Read it with
    # `sudo cat`, as the header says.
    script = ''
      set -euo pipefail
      install -d -m 0755 -o root -g root ${stateDir}
      if [ ! -s ${prefixEnv} ]; then
        umask 077
        tmp="$(${pkgs.coreutils}/bin/mktemp ${stateDir}/.path-prefix.XXXXXX)"
        trap 'rm -f "$tmp"' EXIT
        printf 'HOMELAB_MCP_PATH_PREFIX=%s\n' \
          "$(${pkgs.openssl}/bin/openssl rand -hex 16)" > "$tmp"
        chown root:root "$tmp"
        chmod 0600 "$tmp"
        mv "$tmp" ${prefixEnv}
        trap - EXIT
        echo "generated a new path prefix"
      fi
    '';
  };

  systemd.services.homelab-mcp = {
    description = "MCP server bridging Claude chats to the SilverBullet space";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" "silverbullet.service" "homelab-mcp-path-prefix.service" ];
    requires = [ "homelab-mcp-path-prefix.service" ];

    environment = {
      HOMELAB_MCP_SPACE_ROOT = spaceDir;
      HOMELAB_MCP_INBOX_DIR = "Inbox";
      HOMELAB_MCP_QUEUE_PAGE = "System/Agent Queue.md";
      HOMELAB_MCP_HOST = "127.0.0.1";
      HOMELAB_MCP_PORT = toString port;
      HOMELAB_MCP_PUBLIC_HOST = publicHost;

      # HOMELAB_MCP_PATH_PREFIX is deliberately NOT set here — it comes from
      # EnvironmentFile below. Setting it in both places would make which one
      # wins a systemd-ordering question, and the failure mode of losing that
      # argument is an endpoint served at a guessable path.

      HOMELAB_MCP_CONTEXT_PAGE = "Areas/Agent Context.md";
      HOMELAB_MCP_INCLUDE_SERVICE_INVENTORY = "true";
      HOMELAB_MCP_FLAKE_ROOT = "/home/claude/flakes";

      PYTHONUNBUFFERED = "1"; # audit lines reach journald promptly
    };

    serviceConfig = {
      ExecStart = lib.getExe package;

      # No leading "-": if the prefix file is missing the service must fail to
      # start, not quietly fall back to serving at /mcp.
      EnvironmentFile = prefixEnv;

      # Runs as claude: that user already holds the space ACLs, so no new
      # access is granted to anything by adding this service.
      User = "claude";
      Group = "silverbullet";

      # New files must be born group-writable or their ACL mask caps the other
      # writer at read-only — the same constraint the daybook and autosave
      # units carry (see silverbullet.nix for the full explanation).
      UMask = "0002";

      Restart = "on-failure";
      RestartSec = "10s";

      # --- hardening -----------------------------------------------------
      # This process parses input that originates outside the network, so it
      # gets a tighter sandbox than most services here.
      NoNewPrivileges = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectSystem = "strict";
      ProtectHome = "read-only"; # needs /home/claude/flakes for the inventory
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      ProtectClock = true;
      ProtectHostname = true;
      ProtectProc = "invisible";
      RestrictNamespaces = true;
      RestrictRealtime = true;
      RestrictSUIDSGID = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      SystemCallArchitectures = "native";
      SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];

      # The space is the ONLY writable path. Everything else is read-only even
      # before the application's own path scoping is considered.
      ReadWritePaths = [ spaceDir ];

      # Loopback only — it cannot reach the LAN or the internet, so a bug in
      # request handling cannot be turned into an outbound connection.
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      IPAddressAllow = [ "localhost" ];
      IPAddressDeny = "any";
    };
  };

  # --- gate 1: the source-IP allowlist -----------------------------------
  # A dedicated listener on loopback, reached ONLY by tailscaled. It speaks
  # plain HTTP because tailscaled has already terminated TLS.
  #
  # The allow/deny here REPLACES the inherited set from nginx-access.nix rather
  # than adding to it — ngx_http_access_module directives at an inner level
  # override the outer level, they do not merge. That is the intent: this vhost
  # is the one thing on the box that must answer a WAN address, and it must
  # answer nothing else. The house rule (Tailscale/LAN only) is still correct
  # for every other vhost and is untouched.
  #
  # set_real_ip_from must stay 127.0.0.1: it names who is trusted to assert a
  # client address via PROXY protocol. Widening it would let anyone who can
  # reach this port forge an Anthropic source address and walk through gate 1.
  services.nginx.virtualHosts."homelab-mcp-funnel" = {
    serverName = "_";
    listen = [{
      addr = "127.0.0.1";
      port = proxyPort;
      extraParameters = [ "proxy_protocol" ];
    }];

    extraConfig = ''
      set_real_ip_from 127.0.0.1;
      real_ip_header proxy_protocol;

      allow ${anthropicEgress};
      deny all;
    '';

    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      extraConfig = ''
        # $host, not $http_host: nginx strips the port from $host, so the
        # backend sees a bare "gromit.<tailnet>.ts.net". That is the form the
        # SDK's host allowlist has to match, and the reason server.py lists the
        # bare hostname and not only the "host:*" wildcard.
        #
        # This is what makes the non-default funnel port survivable: a client
        # hitting :8443 sends "Host: gromit.<tailnet>.ts.net:8443", $host drops
        # the ":8443", and the bare entry matches. Switching this to $http_host
        # would break the connector.
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        # MCP's streamable-HTTP transport keeps a long-lived SSE stream open for
        # the server→client direction. Buffering it would hold responses until a
        # buffer filled, which presents as a connector that hangs rather than one
        # that fails, and the default 60s read timeout would cut idle sessions.
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
      '';
    };
  };

  # --- the funnel itself --------------------------------------------------
  # Declarative rather than a one-off `tailscale funnel` at a shell, so the
  # published state lives in the repo and a rebuild re-asserts it.
  #
  # --tls-terminated-tcp + --proxy-protocol=2 is the combination that carries the
  # real client address to the backend; plain --https mode does not, and without
  # it gate 1 above is impossible and the path prefix would be the only defence.
  #
  # This unit FAILS until the `funnel` nodeAttr is granted in the tailnet policy
  # file — that grant is a Tailscale control-plane setting, and there is no API
  # key on this box to do it with. It retries once a minute rather than giving up,
  # so the endpoint comes up on its own when the ACL lands, with no second deploy:
  #
  #   "nodeAttrs": [ { "target": ["autogroup:member"], "attr": ["funnel"] } ]
  systemd.services.homelab-mcp-funnel = {
    description = "Publish homelab-mcp to the internet via Tailscale Funnel";
    wantedBy = [ "multi-user.target" ];
    after = [ "tailscaled.service" "nginx.service" ];
    requires = [ "tailscaled.service" ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;

      ExecStart = ''
        ${pkgs.tailscale}/bin/tailscale funnel --yes --bg \
          --proxy-protocol=2 --tls-terminated-tcp=${toString funnelPort} \
          tcp://127.0.0.1:${toString proxyPort}
      '';

      # Fail closed: stopping or disabling this unit must actually close the
      # door, not leave a published endpoint behind in tailscaled's state.
      ExecStop = "${pkgs.tailscale}/bin/tailscale funnel reset";

      Restart = "on-failure";
      RestartSec = "60s";
    };
  };
}
