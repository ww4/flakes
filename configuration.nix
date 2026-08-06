# Gromit — top-level NixOS configuration.
#
# This file is just the module manifest: each concern lives in its own file
# under ./modules/ (base system) and ./modules/services/ (per-service). To
# try something out, add or comment a single import below and `nixos-rebuild
# test`; roll back with git or the boot menu.
{ ... }:

{
  imports = [
    # Hardware scan (generated — do not edit).
    ./hardware-configuration.nix

    # Base system.
    ./modules/boot.nix
    ./modules/storage.nix
    ./modules/networking.nix
    ./modules/desktop.nix
    ./modules/users.nix
    ./modules/system.nix
    ./modules/packages.nix
    ./modules/virtualisation.nix
    ./modules/home-manager.nix
    ./modules/sops.nix                       # encrypted secrets (sops-nix) — see ./.sops.yaml
    ./modules/nix-remote-builder.nix         # offload builds to wallace (Ryzen 9 5900X) over Tailscale

    # Agent access (scoped, non-root Claude agent) — see modules/agent/README.md.
    ./modules/agent/claude-user.nix
    ./modules/agent/openwebui-secret.nix    # gromit-only: agent's Open WebUI API key (sops)
    ./modules/agent/arr-api-secret.nix      # gromit-only: agent's Sonarr/Radarr/Prowlarr API keys (sops)
    ./modules/agent/jellyfin-api-secret.nix # gromit-only: agent's Jellyfin API key (sops)
    ./modules/agent/sudo.nix
    ./modules/agent/comin.nix               # GitOps applier — rebuilds on merge to main
    ./modules/agent/claude-harness.nix      # root-owned managed settings + guard for the agent
    ./modules/agent/digest.nix              # weekly headless digest (claude -p /catch-up -> ntfy)
    ./modules/agent/claude-config-sync.nix  # hourly pull of the synced global ~/.claude/CLAUDE.md

    # Services.
    ./modules/services/nginx-access.nix     # source-gate all vhosts to Tailscale + LAN (security review 2026-06-04)
    ./modules/services/blocky.nix           # local split-horizon DNS: rosemaryacres.com -> LAN IP so hostnames resolve with the WAN down
    ./modules/services/jellyfin.nix
    ./modules/services/audiobookshelf.nix
    ./modules/services/tandoor.nix
    ./modules/services/pinchflat.nix
    ./modules/services/metube.nix           # yt-dlp web GUI for one-off downloads -> /mnt/fusion/youtube/metube
    ./modules/services/bitcoind.nix
    ./modules/services/fulcrum.nix          # Electrum server (mempool.space backend + Sparrow); indexes the chain
    ./modules/services/mempool.nix          # mempool.space explorer (mariadb+backend+frontend via docker)
    ./modules/services/gyb.nix
    ./modules/services/immich.nix
    ./modules/services/open-webui-proxy.nix  # TLS front door for wallace's Open WebUI (local-LLM chat)
    ./modules/services/vscode-server.nix
    ./modules/services/nextcloud.nix
    ./modules/services/backup.nix
    ./modules/services/notifications.nix
    ./modules/services/daily-reminders.nix   # tappable ntfy nudges (reminder only — claims nothing)
    ./modules/services/media-mirror.nix
    ./modules/services/media-curate.nix      # backed-up tag sweep + YouTube promote (needs Jellyfin key to activate)
    ./modules/services/media-link.nix        # hardlink completed downloads into the library (keeps seeds alive)
    ./modules/services/arr-missing-sweep.nix  # weekly missing-episode/movie search (Sonarr has no recurring one)
    ./modules/services/bub-mirror.nix
    ./modules/services/remote-desktop.nix
    ./modules/services/meshcentral.nix       # MeshCentral server (remote mgmt)
    ./modules/services/meshagent             # MeshAgent: self-manage this host via MeshCentral (the nixpkgs gap)
    ./modules/services/homepage.nix
    ./modules/services/monitoring.nix
    ./modules/services/drive-temps.nix
    ./modules/services/drive-spindown.nix   # park the idle backup-pool USB drives (cooling) — pairs with drive-temps
    ./modules/services/riverwatch.nix
    ./modules/services/alertmanager-ntfy.nix
    ./modules/services/sentinel.nix          # Phase 1 watchdog: detect trouble + notify (no auto-action yet)
    ./modules/services/tracker-signup-watch.nix # low-freq: ntfy when a watched private tracker opens signup
    ./modules/services/snapraid.nix         # inert until parity drive arrives (enable = false)
    ./modules/services/pool-autoremount.nix # self-heals fusion members that drop off the USB bus
    ./modules/services/arr.nix              # Prowlarr + Sonarr + Radarr + Jellyseerr + Gluetun + qBittorrent
    ./modules/services/mam-seedbox.nix      # INERT (enable=false): registers the AirVPN exit IP with MAM's dynamic-seedbox API on change
    ./modules/services/recyclarr.nix        # Daily TRaSH-Guides profile sync into Sonarr/Radarr
    ./modules/services/arr-settings.nix     # declarative Sonarr/Radarr/Prowlarr app settings (recyclarr owns profiles/CFs)
    ./modules/services/decluttarr.nix       # auto-reaps stalled+failed downloads, re-searches
    ./modules/services/lidarr.nix           # music manager (Lidarr)
    ./modules/services/lazylibrarian.nix    # ebook/audiobook manager (Readarr successor)
    ./modules/services/aurral.nix           # Jellyseerr-for-music (Aurral -> Lidarr)
    ./modules/services/forgejo.nix
    ./modules/services/albyhub.nix
    ./modules/services/glances.nix
    ./modules/services/authelia.nix         # SSO / forward-auth gateway (Phase 1)
    ./modules/services/paperless.nix
    ./modules/services/uptime-kuma.nix
    ./modules/services/vaultwarden.nix
    ./modules/services/litestream.nix       # continuous SQLite replication of the vault to B2
    ./modules/services/silverbullet.nix     # markdown notes/tasks space — scheduling-assistant SoT
    ./modules/services/pim.nix              # plain-text calendar vdir + vdirsyncer (Nextcloud two-way, Google RO)
    ./modules/services/homelab-mcp.nix      # MCP connector for Claude-in-the-app — LOOPBACK ONLY until Phase C
    ./modules/agent/daybook.nix             # 09:00/20:00 claude -p bookends: plan the day / review + tomorrow
  ];

  # The NixOS release the system was first installed from. Leave it pinned —
  # see `man configuration.nix`.
  system.stateVersion = "22.11";
}
