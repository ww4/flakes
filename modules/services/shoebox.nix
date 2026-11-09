# shoebox — https://shoebox.rosemaryacres.com
#
# A Google Keep-style card wall over the FLAT markdown subset of the
# SilverBullet space: Keep/ (the 556-page 2013-2026 Keep import), Notes/,
# Inbox/, and the Grocery List. Same files, same disk, different front end.
#
# WHY. SilverBullet has been deployed since 2026-07-09 and, measured on
# 2026-11-08, exactly five pages had ever been created through its editor —
# PatchTest.md, a bare-timestamp quick-note test, "voice typing works now", a
# parody, and one real note (a paint list). The agent wrote the other 817
# straight to the filesystem. The space was the part that worked; the markdown
# IDE in front of it was not: "it's just overwhelming when you look at it"
# (Chris, 2026-11-08). The 2026-08-23 Keep-style redesign changed SilverBullet's
# landing page and button bar but left the editor underneath, so it did not
# move that number.
#
# WHAT IS DELIBERATELY NOT HERE. Journal/ (the daybook), Areas/ (newsdesk),
# Projects/, System/ and Library/ are machine-written and document-shaped; the
# daybook alone would push two cards a day and bury everything Chris wrote.
# The scoping lives in pkgs/shoebox/src/shoebox/paths.py and is enforced by a
# traversal corpus that runs at build time, because this service is
# unauthenticated and a note id is a client-supplied path.
#
# PERMISSIONS. No new plumbing: silverbullet.nix already lays recursive default
# ACLs granting g:silverbullet:rwX over the whole space and keeps the mask at
# rwX via a root timer, so a `shoebox` user in that group can write. UMask 0002
# matches the other writers — a file born 0644 would cap the ACL mask at r--
# and lock the other parties out (the trap that cost four testing deploys in
# July; see the comments in silverbullet.nix).
#
# AUTH. None, matching the SilverBullet vhost beside it. The perimeter is the
# global source-gate in nginx-access.nix (Tailscale/LAN only) plus the tailnet
# ACL. A login here would reintroduce exactly the friction that left the notes
# app unused — Chris declined Authelia on notes.rosemaryacres.com for that
# reason on 2026-07-09, and this is the same content.
{ config, lib, pkgs, ... }:

let
  domain = "rosemaryacres.com";
  spaceDir = "/var/lib/silverbullet";
  port = 3337;
  shoebox = pkgs.callPackage ../../pkgs/shoebox { };
in
{
  users.users.shoebox = {
    isSystemUser = true;
    group = "shoebox";
    # The space's ACLs are keyed on the silverbullet group; joining it is the
    # whole permission story.
    extraGroups = [ "silverbullet" ];
    description = "shoebox card wall";
  };
  users.groups.shoebox = { };

  systemd.services.shoebox = {
    description = "shoebox — card wall over the SilverBullet space";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];

    environment = {
      SHOEBOX_SPACE_DIR = spaceDir;
      SHOEBOX_HOST = "127.0.0.1";
      SHOEBOX_PORT = toString port;
    };

    serviceConfig = {
      ExecStart = "${shoebox}/bin/shoebox";
      User = "shoebox";
      Group = "shoebox";
      SupplementaryGroups = [ "silverbullet" ];
      Restart = "on-failure";
      RestartSec = "5s";

      # New files must be born group-writable or the ACL mask caps the other
      # writers at read-only. Same value the silverbullet and daybook units use.
      UMask = "0002";

      # Hardening. The space is the ONLY path this service may write; it reads
      # its own store path and nothing else of consequence.
      ReadWritePaths = [ spaceDir ];
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      PrivateDevices = true;
      NoNewPrivileges = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      RestrictNamespaces = true;
      RestrictRealtime = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      SystemCallArchitectures = "native";
      SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
      CapabilityBoundingSet = [ ];
    };
  };

  services.nginx.virtualHosts."shoebox.${domain}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      recommendedProxySettings = true;
    };
  };
}
