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
    ./modules/agent/sudo.nix
    ./modules/agent/comin.nix               # GitOps applier — rebuilds on merge to main
    ./modules/agent/claude-harness.nix      # root-owned managed settings + guard for the agent
    ./modules/agent/digest.nix              # weekly headless digest (claude -p /catch-up -> ntfy)
    ./modules/agent/claude-config-sync.nix  # hourly pull of the synced global ~/.claude/CLAUDE.md

    # Services.
    ./modules/services/nginx-access.nix     # source-gate all vhosts to Tailscale + LAN (security review 2026-06-04)
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
    ./modules/services/media-mirror.nix
    ./modules/services/media-curate.nix      # backed-up tag sweep + YouTube promote (needs Jellyfin key to activate)
    ./modules/services/bub-mirror.nix
    ./modules/services/remote-desktop.nix
    ./modules/services/homepage.nix
    ./modules/services/monitoring.nix
    ./modules/services/drive-temps.nix
    ./modules/services/drive-spindown.nix   # park the idle backup-pool USB drives (cooling) — pairs with drive-temps
    ./modules/services/riverwatch.nix
    ./modules/services/alertmanager-ntfy.nix
    ./modules/services/snapraid.nix         # inert until parity drive arrives (enable = false)
    ./modules/services/pool-autoremount.nix # self-heals fusion members that drop off the USB bus
    ./modules/services/arr.nix              # Prowlarr + Sonarr + Radarr + Jellyseerr + Gluetun + qBittorrent
    ./modules/services/recyclarr.nix        # Daily TRaSH-Guides profile sync into Sonarr/Radarr
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
  ];

  # The NixOS release the system was first installed from. Leave it pinned —
  # see `man configuration.nix`.
  system.stateVersion = "22.11";
}
