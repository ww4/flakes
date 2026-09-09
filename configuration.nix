# Gromit — top-level NixOS configuration.
#
# This file is just the module manifest: each concern lives in its own file —
# local under ./modules/, or in the PUBLIC homelab-modules library (`hm.` below,
# 2026-09 modularization; personal values live in ./modules/homelab-values.nix).
# To try something out, add or comment a single import below and `nixos-rebuild
# test`; roll back with git or the boot menu.
{ homelab-modules, ... }:

let hm = homelab-modules.nixosModules; in
{
  imports = [
    # Hardware scan (generated — do not edit).
    ./hardware-configuration.nix

    # Base system.
    hm.boot
    ./modules/homelab-values.nix             # the personal values the hm.* library modules read
    ./modules/storage.nix                    # physical drive mounts (hardware)
    hm.mergerfs-pools                        # assembles homelab.pools into the fusion + backup pools
    ./modules/networking.nix
    ./modules/desktop.nix
    ./modules/users.nix
    hm.system
    ./modules/packages.nix
    ./modules/virtualisation.nix
    ./modules/home-manager.nix
    ./modules/sops.nix                       # encrypted secrets (sops-nix) — see ./.sops.yaml
    ./modules/nix-remote-builder.nix         # offload builds to wallace (Ryzen 9 5900X) over Tailscale
    ./modules/netdiag                        # network diagnostics — gromit is WIRED, so L2 checks work here
    ./modules/netdiag/netwatch.nix           # the guard dog: scheduled netdiag + baseline diff (gromit only)

    # Agent access (scoped, non-root Claude agent) — see modules/agent/README.md.
    ./modules/agent/claude-user.nix
    ./modules/agent/claude-code-pin.nix  # claude-code newer than 26.05 ships — see the file
    ./modules/agent/openwebui-secret.nix    # gromit-only: agent's Open WebUI API key (sops)
    ./modules/agent/arr-api-secret.nix      # gromit-only: agent's Sonarr/Radarr/Prowlarr API keys (sops)
    ./modules/agent/jellyfin-api-secret.nix # gromit-only: agent's Jellyfin API key (sops)
    ./modules/agent/cloudflare-lock3-secret.nix # gromit-only: agent's lock3.net Cloudflare token (sops)
    ./modules/agent/discourse-api-secret.nix # gromit-only: agent's DOW Discourse API key (sops)
    ./modules/agent/digitalocean-secret.nix # gromit-only: agent's DO read-only creds — Spaces S3 + API token (sops)
    ./modules/agent/square-dow-secret.nix   # gromit-only: agent's Square token (DOW account, sops)
    ./modules/agent/sudo.nix
    ./modules/agent/comin.nix               # GitOps applier — rebuilds on merge to main
    ./modules/agent/claude-agent-profile.nix # harness profile (shared agent-modules flake)
    ./modules/agent/digest.nix              # weekly headless digest (claude -p /catch-up -> ntfy)
    ./modules/agent/claude-config-sync.nix  # hourly pull of the synced global ~/.claude/CLAUDE.md
    ./modules/services/content-archives.nix  # weekly rebuild of the podcast transcript corpora
    ./modules/services/podcast-triage.nix    # mine the discovery archives for episodes worth Chris's time
    ./modules/services/blueiris.nix          # CLI for customer Blue Iris NVRs (Craigmyle) over Tailscale
    ./modules/services/newsdesk              # personal RSS news digest (collect -> rank -> claude -p -> page)
    ./modules/services/wx                    # NWS alerts (fast) + Ryan Hall lead time (into the newsdesk)

    # Services.
    hm.nginx-access                         # source-gate all vhosts to Tailscale + LAN — the real perimeter
    hm.nginx-log-paths-check                # eval-time guard: an nginx log outside its writable set = every vhost down
    ./modules/services/blocky.nix           # local split-horizon DNS: rosemaryacres.com -> LAN IP so hostnames resolve with the WAN down
    hm.jellyfin
    hm.audiobookshelf
    hm.tandoor
    hm.pinchflat                            # mediaDir in homelab-values
    hm.metube                               # yt-dlp web GUI for one-off downloads; dir in homelab-values
    ./modules/services/bitcoind.nix
    ./modules/services/fulcrum.nix          # Electrum server (mempool.space backend + Sparrow); indexes the chain
    ./modules/services/mempool.nix          # mempool.space explorer (mariadb+backend+frontend via docker)
    ./modules/services/gyb.nix
    hm.immich                               # media dir + wallace ML offload in homelab-values
    ./modules/services/open-webui-proxy.nix  # TLS front door for wallace's Open WebUI (local-LLM chat)
    ./modules/services/open-notebook.nix     # NotebookLM-alt: chat + podcasts over source docs (surrealdb+app+kokoro)
    ./modules/services/vscode-server.nix
    hm.acme                                 # shared ACME DNS-01 defaults (email + token in homelab-values)
    hm.nextcloud                            # admin/OIDC secrets in homelab-values; pg backups below
    ./modules/services/backup.nix
    ./modules/services/dow-uploads-backup.nix # DOW uploads bucket -> fusion pool (into restic)
    hm.ntfy                                 # write-only anon access; baseUrl/topic in homelab-values
    ./modules/services/daily-reminders.nix   # tappable ntfy nudges (reminder only — claims nothing)
    ./modules/services/media-mirror.nix
    ./modules/services/media-curate.nix      # backed-up tag sweep + YouTube promote (needs Jellyfin key to activate)
    ./modules/services/media-link.nix        # hardlink completed downloads into the library (keeps seeds alive)
    hm.arr-missing-sweep                    # weekly missing-episode/movie search (Sonarr has no recurring one)
    ./modules/services/bub-mirror.nix
    hm.remote-desktop
    ./modules/services/meshcentral.nix       # MeshCentral server (remote mgmt)
    hm.meshagent                            # MeshAgent: self-manage this host via MeshCentral (the nixpkgs gap)
    ./modules/services/homepage.nix
    hm.monitoring                           # Prometheus + Grafana + Alertmanager; site extras in homelab-values
    hm.deploy-drift-watch                   # alerts when Forgejo main is ahead of the deployed commit (the Sep 5 gap)
    hm.mirror-drift-watch                   # alerts when a GitHub push-mirror stops tracking Forgejo (pairs in values)
    hm.drive-temps                          # smartctl temp+SMART exporter; spin-down-safe (ids in values)
    ./modules/services/drive-spindown.nix   # park the idle backup-pool USB drives (cooling) — pairs with drive-temps
    hm.smart-dump                           # read-only FULL SMART dump to agent-readable files (wrapper, NOT raw smartctl)
    ./modules/services/riverwatch.nix
    hm.alertmanager-ntfy                    # webhook shim: alertmanager JSON -> ntfy
    ./modules/services/sentinel.nix          # Phase 1 watchdog: detect trouble + notify (no auto-action yet)
    ./modules/services/tracker-signup-watch.nix # low-freq: ntfy when a watched private tracker opens signup
    ./modules/services/snapraid.nix         # inert until parity drive arrives (enable = false)
    hm.pool-autoremount                     # self-heals pool members that drop off the USB bus (zombie-aware)
    hm.disk-io-watch                        # counts kernel I/O errors + USB resets per device — the QUIET fault shape
    hm.arr                                  # Prowlarr + Sonarr + Radarr + Jellyseerr + Gluetun + qBittorrent
    hm.qbit-vpn-watchdog                    # self-heal the gluetun-IP-change qBit netns wedge (no more manual restarts)
    ./modules/services/qbit-seed-guard.nix  # recover missingFiles torrents after a pool outage + watch tracker H&R rules
    ./modules/services/mam-seedbox.nix      # INERT (enable=false): registers the AirVPN exit IP with MAM's dynamic-seedbox API on change
    hm.recyclarr
    ./modules/services/arr-settings.nix     # declarative Sonarr/Radarr/Prowlarr app settings (recyclarr owns profiles/CFs)
    hm.decluttarr                           # auto-reaps stalled+failed downloads, re-searches
    hm.unpackerr
    hm.lidarr
    hm.lazylibrarian
    ./modules/services/stacks.nix           # physical book catalog (sale scanner, flood losses)
    hm.aurral
    hm.forgejo                              # OIDC secret in homelab-values
    ./modules/services/albyhub.nix
    hm.glances
    hm.authelia                             # SSO: forward-auth + OIDC (one list drives protect AND the access rule)
    hm.paperless                            # secrets + SSO env in homelab-values
    hm.uptime-kuma
    hm.vaultwarden                          # subdomain, env secret + SMTP in homelab-values
    ./modules/services/litestream.nix       # continuous SQLite replication of the vault to B2
    hm.silverbullet                         # markdown notes/tasks space — scheduling-assistant SoT; two-writer values below
    ./modules/services/pim.nix              # plain-text calendar vdir + vdirsyncer (Nextcloud two-way, Google RO)
    ./modules/services/homelab-mcp.nix      # MCP connector for Claude-in-the-app — public via the lock3 VPS jump host
    ./modules/agent/daybook.nix             # 09:00/20:00 claude -p bookends: plan the day / review + tomorrow
  ];

  # The NixOS release the system was first installed from. Leave it pinned —
  # see `man configuration.nix`.
  system.stateVersion = "22.11";

  # Weekly refresh of the podcast transcript corpora. Only LIVE shows: ww4/
  # sh-archive is absent on purpose — Self-Hosted ended at "150: The Last One",
  # so re-fetching it forever would be noise.
  services.contentArchives = {
    enable = true;
    # All eight of the fleet. Only lup + twib were listed until 2026-09-09 —
    # and because of the Environment= splitting bug (see content-archives.nix)
    # only lup was ever actually refreshed. The other six were built once on
    # 2026-08-18 and then frozen, five of them never even pushed to the forge.
    archives = [
      # PERSONAL — Chris listens to these; archived so questions about what was
      # discussed can be answered from the text.
      { name = "lup-archive";          path = "/home/claude/lup-archive"; }
      { name = "twib-archive";         path = "/home/claude/twib-archive"; }
      { name = "audible-archive";      path = "/home/claude/audible-archive"; }
      { name = "wbd-archive";          path = "/home/claude/wbd-archive"; }
      # DISCOVERY — Chris does NOT listen to these. Archived purely so
      # podcast-triage can surface the occasional episode worth his time.
      { name = "tftc-archive";         path = "/home/claude/tftc-archive"; }
      { name = "rhr-archive";          path = "/home/claude/rhr-archive"; }
      { name = "citadel-archive";      path = "/home/claude/citadel-archive"; }
      { name = "btcexplained-archive"; path = "/home/claude/btcexplained-archive"; }
    ];
  };

  # The discovery half of the archive fleet: rank new episodes from the four
  # shows Chris does NOT listen to, then have `claude -p` judge which few are
  # actually worth his time and write them into the SilverBullet queue. Stranded
  # unmerged on origin/content-archives since 2026-08-18 — the archives half
  # (#164) landed without it, so nothing has ever mined the corpus.
  services.podcastTriage = {
    enable = true;
    archives = [
      "/home/claude/tftc-archive"
      "/home/claude/rhr-archive"
      "/home/claude/citadel-archive"
      "/home/claude/btcexplained-archive"
    ];
  };

  # The newsdesk: 86 esoteric RSS sources across eight interest lanes, ranked,
  # judged by `claude -p`, and published as a short edition at
  # digest.rosemaryacres.com/news/. Design + the source catalogue's reasoning:
  # ww4/nixos-homelab-improvements docs/newsdesk-research.md.
  #
  # Defaults carry the schedule (weekday 07:00 brief, Saturday long-read,
  # release notes batched to Mondays) and the quiet-hours notification guard.
  services.newsdesk.enable = true;

  # The weather watch. Three layers on three timers:
  #   wx-alerts   NWS for his coordinates, every 3 minutes. The ONLY thing here
  #               allowed to pierce quiet hours, and only for a tornado warning,
  #               a flash flood emergency or an extreme wind warning.
  #   wx-ryan     Ryan Hall's forecasts -> a `weather` lane in the newsdesk, plus
  #               an ad-hoc ntfy when he is AHEAD of the SPC outlook.
  #   wx-morning  07:05 release of anything quiet hours held, as one summary.
  #
  # Design: ww4/nixos-homelab-improvements docs/weather-watch-research.md.
  # ⚠️ Needs secrets/wx-location.json (his home coordinates — PII) before
  # wx-alerts can do anything; wx-ryan works without it on a cached SPC picture.
  services.wx.enable = true;
}
