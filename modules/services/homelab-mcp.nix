# homelab-mcp — a Model Context Protocol server giving Claude in the app
# (phone, web, desktop) a seat at the SilverBullet space.
#
# PHASE C: published via a jump host on the lock3 VPS. gromit itself stays
# entirely private — this module opens ONE port, on the tailnet interface only,
# to ONE peer.
#
# It has to be public somewhere. Anthropic's cloud connects to the connector
# outbound from 160.79.104.0/21 — it is NOT the phone or the browser that
# connects, even for Desktop and Cowork, because remote connectors are brokered
# through the account:
#   "Claude connects to your remote MCP server from Anthropic's cloud
#    infrastructure, rather than from your local device."
#   "Servers hosted on a private corporate network, behind a VPN, or blocked by
#    a firewall won't connect."
# gromit has no public ingress and is not getting one: the rosemaryacres.com
# names resolve *publicly* to 100.82.117.116, this box's Tailscale CGNAT
# address (see nginx-access.nix). So the public face lives on a VPS we already
# run, and reaches back over WireGuard.
#
# The chain:
#   Anthropic → https://mcp.rosemaryacres.com (lock3 VPS, real LE cert)
#             → GATE 1 there: allow 160.79.104.0/21; deny all
#             → Tailscale → gromit 100.82.117.116:8789
#             → GATE 2 here: allow the VPS's tailnet IP; deny all
#             → homelab-mcp 127.0.0.1:8787
#             → GATE 3 in the app: a 256-bit shared secret, required on every
#               request as Authorization: Bearer (or X-API-Key)
#             → GATE 4, defence in depth: the 128-bit secret path prefix
#
# Gate 3 is new as of 2026-09-01 and is the one that matters. Until then the
# path prefix was the ONLY thing authenticating a caller who got past the IP
# allowlists — a credential in a URL, which every hop writes to its request log.
# Anthropic's own guidance says not to do that. The connector UI gained static
# request headers (beta), so there is now a real credential in a real header,
# and a path prefix appearing in a log is no longer an incident.
#
# Gate 1 lives on the VPS because that is the only place the true client address
# is visible. Nothing proxies in front of it, so plain $remote_addr is the real
# source — no PROXY protocol, no realip, none of the machinery the funnel needed.
#
# WHY NOT TAILSCALE FUNNEL, which this module used to do. Two failures:
#   1. Funnel on 443 made tailscaled BIND <tailnet-ip>:443, colliding with
#      nginx's wildcard 0.0.0.0:443. nginx died with "bind() ... Address already
#      in use" and, having Restart=always, looped — taking EVERY vhost on this
#      box down for two hours, Forgejo included, which is how config changes get
#      here. Moved to 8443 and that was fixed.
#   2. Then Anthropic never connected at all. Four days of nginx and app logs
#      show not one request from their range and not one request carrying the
#      path prefix — so it was the *.ts.net name or the non-standard port, and
#      neither is fixable from this side. Meanwhile the endpoint was found and
#      probed by vulnerability scanners within twelve seconds of going public.
# The VPS route has none of that: a name we control, port 443, an ordinary
# Let's Encrypt certificate.
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

  port = 8787;      # the MCP server itself — loopback, never published
  jumpPort = 8789;  # nginx's tailnet listener, reachable ONLY by the jump host

  spaceDir = "/var/lib/silverbullet";

  stateDir = "/var/lib/homelab-mcp";
  credsEnv = "${stateDir}/credentials.env";

  # This box's Tailscale address. The listener below binds it specifically
  # rather than the wildcard, so the port cannot appear on the LAN or the WAN
  # even if the firewall rule below were ever loosened.
  tailnetIP = "100.82.117.116";

  # The jump host: lock3.sheep-trout.ts.net, the VPS at 104.207.80.108 that
  # terminates TLS for mcp.rosemaryacres.com. It carries tag:jumphost, and the
  # tailnet policy grants that tag exactly one destination — gromit:8789 — so a
  # compromise of that internet-facing box reaches this port and nothing else.
  #
  # A tailnet IP, not a hostname: nginx resolves names once at startup, so a
  # name here would bake in whatever it resolved to at boot. If the VPS is ever
  # rebuilt and rejoins with a different address, THIS LINE must change or the
  # endpoint 403s with no other symptom.
  jumpHostIP = "100.125.31.125";

  # The public hostname clients reach. Not served by this box — it belongs to
  # the VPS — but the app still has to be TOLD it. The MCP SDK auto-enables
  # DNS-rebinding protection whenever the bind address is loopback and then only
  # accepts 127.0.0.1/localhost Host headers, so without this every proxied
  # request comes back 421 Misdirected Request, with no MCP-level error to
  # explain it. See build_transport_security() in server.py.
  publicHost = "mcp.rosemaryacres.com";
in
{
  # --- the connector credentials -----------------------------------------
  # TWO secrets, generated on the box rather than carried in sops. Neither has
  # to survive a restore, nothing else needs to know them, and keeping them out
  # of the repo means the endpoint is not recoverable from git history.
  #
  #   HOMELAB_MCP_MCP_TOKEN     the real authenticator. Sent by the connector as
  #                             `Authorization: Bearer <token>` (or X-API-Key).
  #   HOMELAB_MCP_PATH_PREFIX   defence in depth. Was the ONLY protection until
  #                             2026-09-01; now it just means an unauthenticated
  #                             prober cannot even find the endpoint.
  #
  # Both live in one file because they rotate together: replacing the endpoint
  # means re-entering the connector anyway, so splitting them would only create
  # a state where half the credential changed.
  #
  # To read them: sudo cat /var/lib/homelab-mcp/credentials.env
  # To rotate:    sudo rm /var/lib/homelab-mcp/credentials.env \
  #                 && sudo systemctl restart homelab-mcp-credentials homelab-mcp
  #               ...then update the URL *and* the header in the connector.
  systemd.services.homelab-mcp-credentials = {
    description = "Generate the homelab-mcp connector credentials if absent";
    wantedBy = [ "multi-user.target" ];
    before = [ "homelab-mcp.service" ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };

    # Written to a temp file and moved into place, so ${credsEnv} only ever
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
      if [ ! -s ${credsEnv} ]; then
        umask 077
        tmp="$(${pkgs.coreutils}/bin/mktemp ${stateDir}/.credentials.XXXXXX)"
        trap 'rm -f "$tmp"' EXIT
        {
          printf 'HOMELAB_MCP_PATH_PREFIX=%s\n' \
            "$(${pkgs.openssl}/bin/openssl rand -hex 16)"
          printf 'HOMELAB_MCP_MCP_TOKEN=%s\n' \
            "$(${pkgs.openssl}/bin/openssl rand -hex 32)"
        } > "$tmp"
        chown root:root "$tmp"
        chmod 0600 "$tmp"
        mv "$tmp" ${credsEnv}
        trap - EXIT
        echo "generated new connector credentials"
      fi
    '';
  };

  systemd.services.homelab-mcp = {
    description = "MCP server bridging Claude chats to the SilverBullet space";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" "silverbullet.service" "homelab-mcp-credentials.service" ];
    requires = [ "homelab-mcp-credentials.service" ];

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

      # No leading "-": if the credentials file is missing the service must FAIL
      # to start. With a "-" it would come up serving at a guessable /mcp with no
      # token check either — an unauthenticated endpoint reachable through the
      # jump host, presented as a healthy unit. Fail loudly instead.
      EnvironmentFile = credsEnv;

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

  # --- gate 3: only the jump host may reach this ---------------------------
  # A listener bound to the TAILNET address only. The VPS proxies here over
  # WireGuard; nothing else can, and there is no TLS to terminate because the
  # traffic already crossed an encrypted tunnel.
  #
  # The allow/deny REPLACES the inherited set from nginx-access.nix rather than
  # adding to it — ngx_http_access_module directives at an inner level override
  # the outer level, they do not merge. That is the intent. The house rule
  # (Tailscale + LAN) would let ANY tailnet device reach this port; this vhost
  # narrows that to a single peer, because the whole point of the jump host is
  # that one internet-facing box is the only thing that talks to the connector.
  #
  # No realip and no PROXY protocol here, deliberately. Gate 1 is enforced on
  # the VPS, where $remote_addr is the true client. Trusting a forwarded address
  # at this hop would mean anyone who could reach the port could assert an
  # Anthropic source, so the address that matters here is the peer's own.
  services.nginx.virtualHosts."homelab-mcp-jump" = {
    serverName = "_";
    listen = [{
      addr = tailnetIP;
      port = jumpPort;
    }];

    extraConfig = ''
      allow ${jumpHostIP};
      deny all;
    '';

    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      extraConfig = ''
        # $host, not $http_host: nginx strips the port from $host. The VPS sends
        # "Host: mcp.rosemaryacres.com" (443 is implicit, so no port to strip),
        # and that bare form is what the SDK's host allowlist matches. Changing
        # this to $http_host would break the connector the moment anything
        # reached it on a non-default port.
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

  # The tailnet interface otherwise permits only 22 and 8096 (see
  # modules/networking.nix). Without this the VPS's connections are dropped by
  # the firewall before nginx ever sees them, which looks identical to the
  # allowlist rejecting them.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ jumpPort ];
}
