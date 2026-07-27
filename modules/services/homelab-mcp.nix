# homelab-mcp — a Model Context Protocol server giving Claude in the app
# (phone, web, desktop) a seat at the SilverBullet space.
#
# PHASE B: internal only. Binds 127.0.0.1 and is NOT published. Reachable for
# testing from this box, or over Tailscale via an SSH port-forward. Phase C
# adds the Cloudflare tunnel, the WAF rule restricting to Anthropic's egress
# range, and the secret path prefix from sops — none of which are needed while
# the listener is loopback-only.
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

  port = 8787;
  spaceDir = "/var/lib/silverbullet";
in
{
  systemd.services.homelab-mcp = {
    description = "MCP server bridging Claude chats to the SilverBullet space";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" "silverbullet.service" ];

    environment = {
      HOMELAB_MCP_SPACE_ROOT = spaceDir;
      HOMELAB_MCP_INBOX_DIR = "Inbox";
      HOMELAB_MCP_QUEUE_PAGE = "System/Agent Queue.md";
      HOMELAB_MCP_HOST = "127.0.0.1";
      HOMELAB_MCP_PORT = toString port;

      # Phase B: no path prefix — the listener is loopback-only, so there is
      # nothing for a secret path to protect. Phase C sets this from sops.
      HOMELAB_MCP_PATH_PREFIX = "";

      HOMELAB_MCP_CONTEXT_PAGE = "Areas/Agent Context.md";
      HOMELAB_MCP_INCLUDE_SERVICE_INVENTORY = "true";
      HOMELAB_MCP_FLAKE_ROOT = "/home/claude/flakes";

      PYTHONUNBUFFERED = "1"; # audit lines reach journald promptly
    };

    serviceConfig = {
      ExecStart = lib.getExe package;

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

  # Deliberately NO nginx vhost and NO DNS record in Phase B. Adding a vhost
  # would put this behind the Tailscale/LAN source gate, which sounds harmless
  # but would make it reachable by anything already on the tailnet before the
  # auth story is settled. Loopback until Phase C.
}
