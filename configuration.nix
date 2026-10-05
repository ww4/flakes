# Gromit — top-level NixOS configuration.
#
# This file is just the module manifest: each concern lives in its own file —
# local under ./modules/, or in the PUBLIC homelab-modules library (`hm.` below,
# 2026-09 modularization; personal values live in ./modules/homelab-values.nix).
# To try something out, add or comment a single import below and `nixos-rebuild
# test`; roll back with git or the boot menu.
{ config, homelab-modules, ... }:

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
    ./modules/agent/claude-oauth-token-secret.nix # gromit-only: 1-year subscription OAuth token for the UNATTENDED claude -p units (sops)
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
    ./modules/services/netradio             # Yamaha Net Radio: vTuner stand-in (YCast) + library stations (Icecast/Liquidsoap, on demand)
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
    hm.backup                                # restic critical tier; paths + secrets in homelab-values
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
    hm.snapraid                              # inert until the parity drive arrives (homelab.snapraid.enable = false)
    ./modules/services/guide-preview.nix    # guide.<domain>: the homelab guide site, built by the agent, served from /var/lib/guide-preview
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
    ./modules/services/cd-ripper.nix        # insert a CD in any USB drive → whipper rips it, beets files it in the library, ntfy says so
    ./modules/services/yamaha-ync.nix       # the living-room receiver: JSON API for the radio page + MCP server (public pkg ww4/yamaha-ync)
    ./modules/services/asterisk.nix         # house PBX (pjsip): dial 0 for the switchboard, 1XX for a handset — LAN/tailnet only
    ./modules/services/switchboard.nix      # dial 0: whisper -> intents / claude -p -> piper (FastAGI behind Asterisk)
    ./modules/services/media-gate.nix       # do finished downloads actually parse as media? (ffprobe allowlist, reports only)
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
    # All nine of the fleet. Only lup + twib were listed until 2026-09-09 —
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
      # Jupiter Extras — added 2026-10-05. This is where Clanker Therapy airs
      # (Chris Fisher + Wes on what they have actually built with agents), and
      # the show was in none of the eight, so Clanker Therapy 2 was never
      # downloaded. ⚠️ Extras serves text/plain transcripts and numbers nothing,
      # which the shared build.py could not handle — ww4/extras-archive carries
      # the only copy that can (text/plain in TRANSCRIPT_FORMATS, and
      # "number_from": "date" in its show.json). Its origin has no main yet:
      # the first push is this unit's bootstrap path.
      { name = "extras-archive";       path = "/home/claude/extras-archive"; }
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

  # The living-room Yamaha R-N301 (static 192.168.1.61 — it points at Blocky
  # for the vTuner stand-in; reserve it in the router for MAC 00:a0:de:c4:18:54).
  services.yamaha-ync = { enable = true; host = "192.168.1.61"; name = "Living room"; exposeApiToContainers = true; };  # the Homepage widget
  services.cd-ripper.enable = true;   # USB CD drives on gromit: insert → whipper → beets → /mnt/fusion/Music; radio.rosemaryacres.com/rips/

  # House PBX + the voice switchboard: dial 0 from any registered handset and
  # ask the box a question. Fast intents (status/temps/disk/incidents/time)
  # answer in ~4 s; anything else goes to `claude -p` (~15-60 s, then a
  # call-back if it runs long). Phones/ATAs register with the secrets in
  # /var/lib/asterisk/pjsip-auth.conf (generated at first start).
  # Design + bench numbers: ww4/nixos-homelab-improvements docs/switchboard.md.
  services.homelab-pbx.enable = true;
  services.switchboard = {
    enable = true;
    # Chosen by ear on 2026-09-12 after the dial-8/9 auditions: Kokoro is
    # "clearly superior" for conversation; piper lessac-high keeps an
    # announcement flavour, so it voices call-outs and the time/date intent.
    tts = "kokoro";
    kokoroVoice = "af_heart";
    announceTts = "piper";
    voice = "lessac-high";
    # wallace first (5900X), gromit's own whisper/Kokoro as the fallback when
    # wallace is powered off — hosts/wallace/switchboard-inference.nix.
    remoteWhisperUrls = [ "http://100.66.171.120:8778" ];
    remoteKokoroUrls = [ "http://100.66.171.120:8880" ];
  };

  # --- the radio: this box's values for modules/services/netradio -------------
  # The module itself is generic (options only); everything here is local fact.
  services.netradio = {
    enable = true;
    domain = "radio.rosemaryacres.com";
    title = "Rosemary Acres Radio";
    # The Jellyfin library, and where Lidarr puts new albums (lidarr.nix).
    # Named explicitly rather than left to Jellyfin's library configuration to
    # volunteer. The scanner takes these PLUS whatever Jellyfin reports, and
    # when Jellyfin cannot be reached it falls back to `found = []` — so a
    # rescan during a Jellyfin outage would have quietly emptied the Holiday
    # station, whose 897 tracks all live under XMAS/Music. Discovery stays on;
    # it is now a convenience rather than a dependency (2026-09-29).
    # Rain under whatever else is playing, offered per listener rather than
    # mixed into the broadcast. The seamless loop, not the variety station.
    bedMount = "rainymood";

    libraryRoots = [
      "/mnt/fusion/Music"
      "/mnt/fusion/arr/media/music"
      "/mnt/fusion/XMAS/Music"          # Holiday
      "/mnt/fusion/pinchflat/music"     # audio pulled by pinchflat
    ];

    # The two ambient beds have no album covers to build a tile from, so they
    # get pictures of their own. rainymood.jpg is a crop of rainymood.com's own
    # backdrop — the site whose loop the station plays, branding cropped out.
    # rain.jpg is generated (see the netradio artwork PR), so nothing borrowed.
    stationArt = {
      rain = ./art/netradio/rain.jpg;
      rainymood = ./art/netradio/rainymood.jpg;
      # Everything is the whole library, so it gets the whole band: a lit
      # tuning scale with the needle parked off-centre.
      all = ./art/netradio/all.jpg;
      # Holiday's 897 tracks carry no cover art at all, so there is no mosaic
      # to build — string lights instead.
      holiday = ./art/netradio/holiday.jpg;
    };
    libraryGroup = "media";                 # the library is jellyfin:media 0770/0774
    requiresMounts = [ "mnt-fusion.mount" ];

    # One voice for the whole house: the switchboard's Kokoro choice and its
    # URL order (wallace first, gromit's own container last).
    tts = {
      voice = config.services.switchboard.kokoroVoice;
      urls = config.services.switchboard.remoteKokoroUrls ++ [ "http://127.0.0.1:8880" ];
      afterUnits = [ "docker-open-notebook-kokoro.service" ];
    };

    # gromit's analog out (Realtek ALC887-VD, card 0). Muted at boot like the
    # receiver: the stream runs and the jack is silent until the remote says so.
    speaker = {
      enable = true;
      label = "Gromit speakers";
      defaultMount = "rainymood";           # the seamless loop, not the five-bed variety station
      # Chris set this by ear on the rain loop (2026-09-27): where it should come
      # up, with plenty of headroom left above it.
      startVolume = 70;
      # A hard ceiling, because the level has twice ended up at 100% with
      # nothing in the log that put it there — 2026-09-26, and again at about
      # 03:14 on 2026-10-01, which woke Chris out of a dead sleep. The ceiling
      # was the answer last time and it never bit: the option defaults to 100,
      # so it was never actually a ceiling. 70 is where he listens, and nothing
      # on this box has a reason to go above it.
      maxVolume = 70;
    };

    # The living-room R-N301, via modules/services/yamaha-ync.nix.
    receiver = {
      enable = config.services.yamaha-ync.enable;
      label = config.services.yamaha-ync.name;
      apiUrl = "http://127.0.0.1:${toString config.services.yamaha-ync.apiPort}";
      afterUnits = [ "yamaha-ync-api.service" ];
      # The R-N301's spec lists MP3, WMA and MPEG4 AAC — that last one is AAC-LC.
      # Deliberately NOT he-aac: the 32 kbps SecureNetSystems streams are
      # HE-AACv2 (measured 2026-09-27) and a 2014 decoder will not take them, so
      # they stay out of the receiver's menu and off the phone's Internet Radio only
      # for the receiver.
      codecs = [ "mp3" "wma" "aac-lc" ];
    };

    # The receiver's Net Radio input: Blocky answers the vTuner names with the
    # LAN address nginx is on (the same value as blocky.nix's proxyIP — reading
    # it back from services.blocky.settings while adding to it is an infinite
    # recursion, so keep the two together).
    vtuner = { enable = true; address = "192.168.1.65"; };

    # Audio analysis offloaded to wallace when it is up (the 5900X is 4-5x
    # faster); eight local workers keep twelve server processes fed over the
    # tailnet, and fall back to this box's four cores when it is off.
    profile = { workers = 8; remotes = [ "http://100.66.171.120:8790" ]; };

    # The admin page writes a feed's rule from its description with `claude -p`.
    compile = { enable = true; workingDirectory = "/home/claude/nixos-homelab-improvements"; };

    lastfmEnvFile = config.sops.secrets."lastfm-env".path;

    # Local stations, verified 2026-09-27 by fetching each one and reading the
    # codec off it rather than trusting a directory listing.
    #
    # ⚠️ The two country stations are HE-AACv2 at 32 kbps (SecureNetSystems).
    # The R-N301's spec says "MPEG4 AAC", which is AAC-LC — HE-AACv2 is a
    # different profile and a 2014 net-radio decoder usually refuses it. They
    # will play on the phone and the browser; the receiver may well not take
    # them. The MP3 ones play everywhere.
    extraInternetRadio = [
      # WLXO Mount Sterling, classic country. hankthelegend.com
      { name = "Hank FM 105.5";   url = "http://ice9.securenetsystems.net/WLXO"; codec = "he-aac"; }
      # WFKY Frankfort, "Froggy" country. froggykycountry.com
      { name = "Froggy 104.9";    url = "http://ice9.securenetsystems.net/WFKY"; codec = "he-aac"; }
      # WMMT Whitesburg — Appalshop's Possum Radio. MP3 128k.
      # ⚠️ NOT the URL in the Radio Browser index: that one answers as "XB
      # Radio", a stale mount. This is what TuneIn resolves, reliability 100.
      { name = "WMMT 88.7 Possum Radio"; url = "http://mira.streamerr.co/listen/wmmt_88.7fm/radio.mp3"; }
      # WETS Johnson City. HD1 is the main service, HD2 is its Americana stream.
      { name = "WETS 89.5";       url = "http://wets-fm.streamguys1.com/live-1"; }
      { name = "WETS Americana";  url = "http://wets-fm.streamguys1.com/live-2"; }
    ];

    # The Jellyfin already running on this box: it says where the music is (it
    # named /mnt/fusion/XMAS/Music and /mnt/fusion/pinchflat/music, which the
    # hand-written roots above had missed), and the page's heart button mirrors
    # to its favourites in both directions.
    jellyfin = {
      enable = true;
      keyFile = config.sops.secrets."jellyfin-api-netradio".path;
    };
  };

  # Genre tags (beets lastgenre) and the DJ's similar-artist lookups. Read-only
  # use. Owner netradio; group users so the agent's catalogue jobs (as claude)
  # can read it too — a low-value key. Handed over via the secrets-inbox
  # 2026-09-16; edit with `sops secrets/lastfm-env.yaml`.
  # netradio's own view of the Jellyfin key. The agent's copy in
  # modules/agent/jellyfin-api-secret.nix is owner claude 0400 and stays that
  # way; sops-nix can expose the same key twice with different owners.
  sops.secrets."jellyfin-api-netradio" = {
    sopsFile = ./secrets/jellyfin-api.yaml;
    key = "jellyfin-api";
    owner = "netradio";
    mode = "0400";
  };

  sops.secrets."lastfm-env" = {
    sopsFile = ./secrets/lastfm-env.yaml;
    key = "lastfm-env";
    owner = "netradio";
    group = "users";
    mode = "0440";
  };
}
