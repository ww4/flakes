# Homepage dashboard — self-hosted launchpage at https://rosemaryacres.com
# (and www.rosemaryacres.com, which redirects to apex). Containerised via
# virtualisation.oci-containers; all config is fully declarative — every
# YAML file is bind-mounted read-only from the Nix store.
#
# Live-data widgets (Jellyfin now-playing, Nextcloud quota, Immich library
# stats, etc.) read API keys from env vars via Homepage's {{HOMEPAGE_VAR_*}}
# template syntax. The keys live in sops (secrets/homepage-env.yaml)
# (root:root, 0600) in dotenv format, then `sudo systemctl restart
# docker-homepage`:
#
#   HOMEPAGE_VAR_JELLYFIN_KEY=...
#   HOMEPAGE_VAR_IMMICH_KEY=...
#   HOMEPAGE_VAR_NEXTCLOUD_USER=chris
#   HOMEPAGE_VAR_NEXTCLOUD_PASS=...      # an app-password from Nextcloud
#   HOMEPAGE_VAR_AUDIOBOOKSHELF_KEY=...
#   HOMEPAGE_VAR_TANDOOR_KEY=...
#
# How to mint each key:
#   - Jellyfin       → Dashboard → Advanced → API Keys → +
#   - Immich         → Account Settings → API Keys → New API Key
#   - Nextcloud      → Personal → Security → Devices & sessions → "Create
#                      new app password" (use that, not the login password)
#   - Audiobookshelf → Settings → Users → click user → API Token
#   - Tandoor        → User dropdown → API Token → Create Token
#
# DNS prerequisite on Cloudflare:
#   A   rosemaryacres.com       100.82.117.116    (Tailscale IP, proxy off)
#   A   www.rosemaryacres.com   100.82.117.116    (Tailscale IP, proxy off)
{ config, lib, pkgs, ... }:
let
  hostname = "rosemaryacres.com";
  port = 3000;

  # ── Layout ───────────────────────────────────────────────────────────────
  # Groups are assigned to TABS. Tab order follows the order groups appear in
  # this block, and the first one ("Home") is what opens on load. Home is a
  # hand-picked Favorites row over the household apps; everything else is one
  # tap away. Quicklaunch (the search box) still finds services on *every*
  # tab, so nothing is lost by moving a group off Home.
  #
  # ⚠️ Every service group MUST be listed here. Homepage renders laid-out
  # groups into #layout-groups and any group *missing* from this block into a
  # separate #services container that comes AFTER all of them — so a forgotten
  # group silently teleports to the bottom of the page. That is exactly how
  # "Productivity" and "Gromit (live)" ended up stranded below Riverwatch
  # before 2026-10-19. If you add a group to services.yaml, add it here too.
  #
  # ⚠️ A group with no `tab:` is shown on EVERY tab (upstream behaviour), so
  # leaving one out does not hide it — it duplicates it.
  settingsYaml = pkgs.writeText "homepage-settings.yaml" ''
    title: Gromit
    theme: dark
    # slate (blue-tinted) rather than gray — the flat neutral grey is what
    # made the page look washed out. The real colour comes from customCss.
    color: slate
    headerStyle: clean
    hideVersion: true
    # Card translucency/blur is handled in customCss rather than with the
    # built-in `cardBlur:` knob, so one mechanism owns the glass effect
    # instead of two fighting over the same surface.

    layout:
      # ── Home ── what actually gets opened
      # Favorites is NOT a themed group: it is the four things Chris reaches
      # for most, and it is expected to be edited by hand whenever that
      # changes. Homepage tracks nothing — no click counter, no usage
      # ordering, no persistence of any kind on a tile click (checked against
      # src/components/services/item.jsx, 2026-10-19) — so a "top apps" row
      # can only ever be a hand-kept list.
      Favorites:
        tab: Home
        style: row
        columns: 4
        useEqualHeights: true
      # Four tiles, one row, now that Nextcloud and Tandoor have moved up to
      # Favorites rather than appearing twice on the same tab.
      Household:
        tab: Home
        style: row
        columns: 4
        useEqualHeights: true
      # Last on the tab, under Household: the tiles are what Chris came to the
      # page for, and this is what he reads once he is already here.
      # Full width, so columns: 1.
      Briefing:
        tab: Home
        style: row
        columns: 1

      # ── Media ── watching on top, the acquisition machinery below it
      # Media is deliberately DUPLICATED: its four tiles also appear in
      # Favorites. Homepage has no "show this group on two tabs" option
      # (a group with no `tab:` appears on ALL of them, which is worse), so
      # the services.yaml entries are simply written twice. Each copy polls
      # its own widget — negligible for services on this host, but worth
      # knowing before duplicating anything that costs a real API call.
      Media:
        tab: Media
        style: row
        columns: 4
      Media Mgmt:
        tab: Media
        style: row
        columns: 3
        useEqualHeights: true

      # ── Lab ── things that are interesting but not daily
      AI:
        tab: Lab
        style: row
        columns: 2
      Bitcoin:
        tab: Lab
        style: row
        columns: 2
      Infrastructure:
        tab: Lab
        style: row
        columns: 2
        useEqualHeights: true

      # ── System ── host health, dashboards, alerts
      Gromit (live):
        tab: System
        style: row
        columns: 5
        useEqualHeights: true
      Temperatures:
        tab: System
        style: row
        columns: 1
      Monitoring:
        tab: System
        style: row
        columns: 3
        useEqualHeights: true
      Recent Alerts:
        tab: System
        style: row
        columns: 1

      # ── Weather ── the strip first, the river underneath it
      # Both want the full width: the strip is a wide single card and the
      # Riverwatch embed is a 640px Grafana panel.
      Weather:
        tab: Weather
        style: row
        columns: 1
      Riverwatch:
        tab: Weather
        style: row
        columns: 1

      # Bookmark groups (bookmarks.yaml) need a tab for the same reason
      # service groups do — without one they are drawn on every tab.
      Code:
        tab: Lab
      Admin:
        tab: Lab
  '';

  # ── custom.css ───────────────────────────────────────────────────────────
  # Homepage reads /app/config/custom.css and injects it after its own
  # stylesheet. Everything below targets class names Homepage actually emits —
  # they were read off the live DOM on 2026-10-19, not guessed:
  #
  #   #page_wrapper  #inner_wrapper (the scroll container, NOT the document)
  #   #information-widgets  #widgets-wrap
  #   ul#myTab[role=tablist] > li[role=presentation] > button[role=tab]
  #   #layout-groups / #services  .services-group  h2.service-group-name
  #     ul.services-list > li.service > .service-card
  #       .service-title > .service-icon + .service-title-text > .service-name
  #       .service-container > .service-block > div.font-thin / div.font-bold
  #   #bookmarks  .bookmark-group  h2.bookmark-group-name  .bookmark-list
  #
  # ⚠️ Homepage styles cards with Tailwind utilities (dark:bg-white/5 and
  # friends). A bare `.service-card { background: … }` loses to those, so the
  # card rules are scoped under the #layout-groups / #services ids to win on
  # specificity rather than by carpet-bombing with !important. Where a single
  # Tailwind utility has to be beaten in place (margins on #information-widgets,
  # text-xl on the group headings) !important is used deliberately and noted.
  #
  # ⚠️ The tablist carries `sm:flex`, i.e. it is NOT flex below 640px — the
  # tabs stack into full-width rows and eat half a phone screen. The phone
  # block at the bottom forces it back to a single scrollable row.
  customCss = pkgs.writeText "homepage-custom.css" ''
    /* ===================================================================
       Gromit — rosemaryacres.com
       Dark slate base with two soft colour blooms, frosted cards on top.
       =================================================================== */

    /* --- 1. Background ------------------------------------------------ */
    /* The gradient is painted by a fixed pseudo-element so it stays put
       while #inner_wrapper scrolls underneath it. Page content sits in a
       .container with z-10, so z-index 0 here keeps it behind everything. */
    html,
    body {
      background-color: #0b1120 !important;
    }

    #page_wrapper::before {
      content: "";
      position: fixed;
      inset: 0;
      z-index: 0;
      pointer-events: none;
      background:
        radial-gradient(55rem 38rem at 8% -10%, rgba(99, 102, 241, 0.30), transparent 62%),
        radial-gradient(44rem 30rem at 92% 2%, rgba(13, 148, 136, 0.22), transparent 64%),
        radial-gradient(62rem 44rem at 50% 114%, rgba(56, 89, 180, 0.20), transparent 66%),
        linear-gradient(168deg, #0b1120 0%, #111c33 48%, #0b1120 100%);
    }

    /* --- 2. Header strip ---------------------------------------------- */
    /* The banner is NOT a card. It was briefly given the same glass panel as
       the tiles below it, which made it compete with them and boxed in the
       one part of the page that should read as sitting directly on the
       background. No panel, no border, no blur — just spacing.
       Tailwind puts m-5/sm:m-9 on this; !important to reclaim it. */
    #information-widgets {
      margin: 1.1rem 1.5rem 0 !important;
      padding: 0;
      background: none;
      border: 0;
      box-shadow: none;
    }

    /* The search field is the one element here that still needs an edge —
       it is an input, and an input with no boundary is not obviously
       typable. Kept deliberately quieter than a service card. */
    #information-widgets input {
      border-radius: 11px !important;
    }

    /* --- 3. Tab bar ---------------------------------------------------- */
    #myTab {
      display: flex !important;
      /* The tablist is a child of a `flex flex-col … min-h-screen` container,
         so with the default flex-shrink the long service list squeezes it
         down to a ~12px sliver. */
      flex: none;
      gap: 0.25rem;
      margin: 0.85rem 1.5rem 0.1rem;
      padding: 0.3rem;
      border-radius: 14px;
      background: rgba(148, 163, 184, 0.06) !important;
      border: 1px solid rgba(226, 232, 240, 0.08);
      box-shadow: 0 10px 30px -20px rgba(0, 0, 0, 0.9);
      backdrop-filter: blur(16px) saturate(140%);
      -webkit-backdrop-filter: blur(16px) saturate(140%);
      overflow-x: auto;
      scrollbar-width: none;
    }

    #myTab::-webkit-scrollbar {
      display: none;
    }

    /* Upstream gives each li `h-10 w-full` and the button `m-1`. Leave the
       40px height alone — an earlier `height: auto` here collapsed the whole
       bar to a 12px sliver — but drop w-full: five tabs sharing 1680px makes
       each pill 350px wide, which reads as a stretched toolbar rather than a
       tab bar. Content-width pills, left-aligned, on every screen size; the
       bar itself still spans the width to line up with the header strip. */
    #myTab > li {
      align-items: stretch;
      flex: 0 0 auto;
      width: auto !important;
    }

    #myTab button[role="tab"] {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      margin: 0 !important;
      padding: 0 0.95rem;
      border-radius: 10px !important;
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      white-space: nowrap;
      color: rgba(203, 213, 225, 0.6);
      transition: color 0.18s ease, background 0.18s ease, box-shadow 0.18s ease;
    }

    #myTab button[role="tab"]:hover {
      color: #e2e8f0;
    }

    #myTab button[aria-selected="true"] {
      background: linear-gradient(180deg, rgba(129, 140, 248, 0.32), rgba(99, 102, 241, 0.18)) !important;
      color: #f1f5f9;
      box-shadow:
        0 1px 0 rgba(255, 255, 255, 0.14) inset,
        0 6px 18px -9px rgba(99, 102, 241, 0.8);
    }

    /* --- 4. Group headings --------------------------------------------- */
    /* Small caps + a rule out to the right edge, so the eye reads a section
       break instead of another big title competing with the card names.
       text-xl has to be overridden in place, hence !important here. */
    h2.service-group-name,
    h2.bookmark-group-name {
      display: flex;
      flex: 1 1 auto;
      align-items: center;
      gap: 0.55rem;
      font-size: 0.74rem !important;
      font-weight: 600 !important;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: rgba(203, 213, 225, 0.68) !important;
    }

    h2.service-group-name::before,
    h2.bookmark-group-name::before {
      content: "";
      flex: none;
      width: 0.28rem;
      height: 0.95rem;
      border-radius: 999px;
      background: linear-gradient(180deg, #818cf8, #22d3ee);
    }

    h2.service-group-name::after,
    h2.bookmark-group-name::after {
      content: "";
      flex: 1 1 auto;
      height: 1px;
      /* Fixed-length fade: on a 1680px row a `to transparent` gradient is
         already invisible 200px in, so the rule has to stop deliberately
         rather than stretch with the container. */
      background: linear-gradient(90deg, rgba(148, 163, 184, 0.4), rgba(148, 163, 184, 0) 260px);
    }

    /* --- 5. Cards ------------------------------------------------------ */
    #layout-groups .service-card,
    #services .service-card,
    #bookmarks li.bookmark > a {
      border-radius: 14px !important;
      border: 1px solid rgba(226, 232, 240, 0.09);
      background: linear-gradient(
        180deg,
        rgba(255, 255, 255, 0.075),
        rgba(255, 255, 255, 0.035)
      ) !important;
      box-shadow:
        0 1px 0 rgba(255, 255, 255, 0.07) inset,
        0 10px 26px -16px rgba(0, 0, 0, 0.85) !important;
      backdrop-filter: blur(14px) saturate(135%);
      -webkit-backdrop-filter: blur(14px) saturate(135%);
      transition:
        transform 0.18s ease,
        border-color 0.18s ease,
        box-shadow 0.18s ease,
        background 0.18s ease;
    }

    /* Lift on hover only where there is a real pointer — on a phone every
       card would otherwise be stuck in its hover state after a tap. */
    @media (hover: hover) {
      #layout-groups .service-card:hover,
      #services .service-card:hover,
      #bookmarks li.bookmark > a:hover {
        transform: translateY(-2px);
        border-color: rgba(129, 140, 248, 0.45);
        box-shadow:
          0 1px 0 rgba(255, 255, 255, 0.1) inset,
          0 18px 34px -16px rgba(0, 0, 0, 0.9),
          0 0 0 1px rgba(129, 140, 248, 0.2) !important;
      }
    }

    /* Service name a touch larger; the description is secondary. */
    .service-name {
      font-size: 0.86rem !important;
      letter-spacing: 0.005em;
    }

    /* --- 6. Stat chips -------------------------------------------------- */
    /* .service-block is the labelled stat tile. Widgets that render a list
       instead (customapi display: list, e.g. Library radio) emit sibling
       divs carrying Tailwind's rounded-sm, so they are matched by shape. */
    #layout-groups .service-block,
    #services .service-block,
    #layout-groups .service-container div[class*="rounded-sm"],
    #services .service-container div[class*="rounded-sm"] {
      border-radius: 10px !important;
      background: rgba(2, 6, 23, 0.4) !important;
      border: 1px solid rgba(226, 232, 240, 0.055);
    }

    .service-block .font-thin {
      font-size: 0.95rem !important;
      font-weight: 500 !important;
      color: #e2e8f0 !important;
    }

    .service-block .font-bold {
      font-size: 0.6rem !important;
      letter-spacing: 0.09em;
      color: rgba(148, 163, 184, 0.78) !important;
    }

    /* --- 6b. Embedded frames -------------------------------------------- */
    /* ⚠️ Iframe heights live HERE, not in the widget's `classes`.
       Homepage builds the iframe as `rounded-sm w-full ''${classes}` and
       `classes` REPLACES its default height classes. Tailwind compiles
       arbitrary values (h-[640px]) only when they appear in its own source
       scan, and our YAML never does — so the class resolved to nothing and
       every embed fell back to the browser's intrinsic 150px iframe height.
       Measured live on 2026-10-19: the river graph had been drawing at 150px
       instead of 640px, and the temperature panel at 150px instead of 360px.
       Keying off the service `id` (which Homepage puts verbatim on the
       <li class="service">) is the documented way to do this and cannot
       silently evaporate the same way. */
    #embed-river iframe {
      height: 640px;
    }

    #embed-temps iframe {
      height: 360px;
    }

    #embed-wx-strip iframe {
      height: 240px;
    }

    /* 215px, not the 190px this started at: the card measured 197px of
       content and the overflowing 7px drew a scrollbar INSIDE the tile. The
       card itself also sets overflow:hidden now, so this height only has to
       be generous enough that nothing is actually clipped. */
    #embed-briefing iframe {
      height: 215px;
    }

    @media (max-width: 640px) {
      /* A 640px graph is most of a phone screen, and the strip's ten-day row
         wraps to two rows of five below this width. */
      #embed-river iframe {
        height: 420px;
      }

      #embed-temps iframe {
        height: 300px;
      }

      #embed-wx-strip iframe {
        height: 340px;
      }

      /* The two panels stack below 640px, and the issue pills wrap: measured
         natural height is 333px at 360-430px wide and 363px at 320px (three
         rows of pills). 375px covers the worst case with headroom. The panels
         carry flex:1, so a quiet day grows them rather than leaving a gap. */
      #embed-briefing iframe {
        height: 375px;
      }
    }

    /* --- 7. Phone ------------------------------------------------------- */
    @media (max-width: 640px) {
      /* Tighter gutters: on a 390px screen the default m-5 throws away
         10% of the width. */
      #information-widgets {
        margin: 0.6rem 0.7rem 0 !important;
        padding: 0;
      }

      #myTab {
        margin: 0.6rem 0.7rem 0.1rem;
        padding: 0.25rem;
        /* Enough of tab 4 stays visible to show the row scrolls. */
        justify-content: flex-start;
      }

      #myTab button[role="tab"] {
        padding: 0 0.8rem;
        font-size: 0.7rem;
        letter-spacing: 0.1em;
      }

      #layout-groups,
      #services,
      #bookmarks {
        margin: 0.5rem 0.7rem !important;
      }

      #widgets-wrap {
        gap: 0.1rem 0.4rem;
      }

      #information-widgets .text-xs {
        font-size: 0.66rem;
      }

      /* Five resources (cpu, mem, /, fusion, backup) at Homepage's default
         117px each fit only two per row on a 390px phone, so the block costs
         three rows / 122px before a single service card appears. Trimming the
         icon gap (ml-3) and the 85px text column to ~104px per item fits
         three per row, i.e. two rows. Measured on the live page 2026-10-19. */
      #information-widgets .information-widget-resource {
        margin-right: 0.35rem !important;
        padding-top: 0.2rem !important;
        padding-bottom: 0.2rem !important;
      }

      #information-widgets .information-widget-resource > div {
        margin-left: 0.4rem !important;
        min-width: 74px !important;
      }
    }

    /* --- 8. Motion ------------------------------------------------------ */
    @media (prefers-reduced-motion: reduce) {
      #layout-groups .service-card,
      #services .service-card,
      #bookmarks li.bookmark > a,
      #myTab button[role="tab"] {
        transition: none;
      }

      @media (hover: hover) {
        #layout-groups .service-card:hover,
        #services .service-card:hover,
        #bookmarks li.bookmark > a:hover {
          transform: none;
        }
      }
    }
  '';

  servicesYaml = pkgs.writeText "homepage-services.yaml" ''
    # ── Briefing ─────────────────────────────────────────────────────────
    # The LAST group on the Home tab (see settings.layout — group order on a
    # tab comes from the layout block, not from the order here): open sentinel
    # conditions, today's daybook plan, and the newsdesk edition as a reading
    # teaser. The tiles above are what the page is for; this is what you read
    # once you are already on it. Rendered every five minutes by
    # `briefing card` (modules/services/briefing) and served by nginx off this
    # same vhost, so the iframe is same-origin.
    #
    # ⚠️ NO `allowScrolling: "no"` here, unlike the other embeds. Homepage sets
    # `pointer-events: none` on the frame when that option is present, which
    # would make every link on this card dead — and this card is entirely
    # about being clicked.
    - Briefing:
        - Today:
            description: What is open, what today is about, and something to read
            href: https://notes.rosemaryacres.com
            icon: mdi-script-text-outline
            id: embed-briefing
            widget:
              type: iframe
              name: Briefing
              src: /briefing/card.html
              classes: w-full
              referrerPolicy: same-origin

    # ── Favorites ────────────────────────────────────────────────────────
    # The top-apps row, and the only group here chosen by use rather than by
    # theme. Hand-kept on purpose: Homepage records nothing when a tile is
    # clicked, so there is no usage data to sort by and never will be without
    # a backend it does not have.
    #
    # Every tile below is a DUPLICATE of its entry in a themed group further
    # down — Tandoor and Nextcloud from Household, Jellyfin and Immich from
    # Media. The themed groups stay complete; this is an additional way in,
    # not a move. ⚠️ Edit a widget here and you must edit its twin too; a
    # rotated API key breaks both copies, and a key fixed in only one place
    # leaves a tile that looks fine on one tab and empty on another.
    - Favorites:
        - Tandoor:
            description: Recipes
            href: https://recipes.rosemaryacres.com
            icon: tandoor-recipes.png
            widget:
              type: tandoor
              url: https://recipes.rosemaryacres.com
              key: {{HOMEPAGE_VAR_TANDOOR_KEY}}
        - Nextcloud:
            description: Files, calendar, notes
            href: https://cloud.rosemaryacres.com
            icon: nextcloud.png
            widget:
              type: nextcloud
              url: https://cloud.rosemaryacres.com
              username: "{{HOMEPAGE_VAR_NEXTCLOUD_USER}}"
              password: "{{HOMEPAGE_VAR_NEXTCLOUD_PASS}}"
        - Jellyfin:
            description: Movies, TV, music
            href: https://jellyfin.rosemaryacres.com
            icon: jellyfin.png
            widget:
              type: jellyfin
              url: https://jellyfin.rosemaryacres.com
              key: {{HOMEPAGE_VAR_JELLYFIN_KEY}}
              enableNowPlaying: true
              enableBlocks: true
        - Immich:
            description: Photos & videos
            href: https://photos.rosemaryacres.com
            icon: immich.png
            widget:
              type: immich
              url: https://photos.rosemaryacres.com
              key: {{HOMEPAGE_VAR_IMMICH_KEY}}
              version: 2

    - Media:
        - Library radio:
            description: The house radio, and the living-room receiver
            href: https://radio.rosemaryacres.com
            icon: mdi-radio
            widget:
              # the receiver's JSON API (yamaha-ync-api on the host-gateway
              # port, opened to br-+ by services.yamaha-ync.exposeApiToContainers)
              type: customapi
              url: http://host.docker.internal:8791/status
              refreshInterval: 10000
              method: GET
              display: list
              mappings:
                - field: power
                  label: Receiver
                - field: input
                  label: Input
                - field:
                    now_playing: station
                  label: Station
                - field:
                    now_playing: track
                  label: Now playing
                - field: volume
                  label: Volume
        - Jellyfin:
            description: Movies, TV, music
            href: https://jellyfin.rosemaryacres.com
            icon: jellyfin.png
            widget:
              type: jellyfin
              url: https://jellyfin.rosemaryacres.com
              key: {{HOMEPAGE_VAR_JELLYFIN_KEY}}
              enableNowPlaying: true
              enableBlocks: true
        - Audiobookshelf:
            description: Audiobooks & podcasts
            href: https://abs.rosemaryacres.com
            icon: audiobookshelf.png
            widget:
              type: audiobookshelf
              url: https://abs.rosemaryacres.com
              key: {{HOMEPAGE_VAR_AUDIOBOOKSHELF_KEY}}
        - Immich:
            description: Photos & videos
            href: https://photos.rosemaryacres.com
            icon: immich.png
            widget:
              # Immich binds 127.0.0.1 only, so the container can't reach
              # it on the Tailscale IP — go through nginx instead.
              # version: 2 picks the new /api/server/{statistics,version}
              # endpoints (Immich 1.85+ dropped /api/server-info/*).
              type: immich
              url: https://photos.rosemaryacres.com
              key: {{HOMEPAGE_VAR_IMMICH_KEY}}
              version: 2

    # *arr stack — widgets pull live stats from each service via API.
    # Mint API keys in each UI:
    #   Sonarr/Radarr/Prowlarr   Settings → General → API Key
    #   Jellyseerr               Settings → "API Key" panel
    # qBittorrent uses username/password (no API key model).
    # Add to the sops file (secrets/homepage-env.yaml):
    #   HOMEPAGE_VAR_PROWLARR_KEY=...
    #   HOMEPAGE_VAR_SONARR_KEY=...
    #   HOMEPAGE_VAR_RADARR_KEY=...
    #   HOMEPAGE_VAR_JELLYSEERR_KEY=...
    #   HOMEPAGE_VAR_LIDARR_KEY=...     # Lidarr → Settings → General → API Key
    #   HOMEPAGE_VAR_QBITTORRENT_USER=admin
    #   HOMEPAGE_VAR_QBITTORRENT_PASS=...
    # Then: sudo systemctl restart docker-homepage
    - Media Mgmt:
        - Prowlarr:
            description: Indexer hub
            href: https://prowlarr.rosemaryacres.com
            icon: prowlarr.png
            widget:
              type: prowlarr
              url: http://prowlarr:9696
              key: {{HOMEPAGE_VAR_PROWLARR_KEY}}
        - Sonarr:
            description: TV
            href: https://sonarr.rosemaryacres.com
            icon: sonarr.png
            widget:
              type: sonarr
              url: http://sonarr:8989
              key: {{HOMEPAGE_VAR_SONARR_KEY}}
        - Radarr:
            description: Movies
            href: https://radarr.rosemaryacres.com
            icon: radarr.png
            widget:
              type: radarr
              url: http://radarr:7878
              key: {{HOMEPAGE_VAR_RADARR_KEY}}
        - Jellyseerr:
            description: Requests
            href: https://requests.rosemaryacres.com
            icon: jellyseerr.png
            widget:
              type: jellyseerr
              url: http://jellyseerr:5055
              key: {{HOMEPAGE_VAR_JELLYSEERR_KEY}}
        - Music Requests:
            description: Request music (Aurral → Lidarr)
            href: https://music.rosemaryacres.com
            icon: mdi-music
            # Link-only: Aurral has no Homepage widget integration.
        - qBittorrent:
            description: Downloads (via AirVPN)
            href: https://qbittorrent.rosemaryacres.com
            icon: qbittorrent.png
            widget:
              # qBit's WebUI lives in Gluetun's netns; both Homepage and
              # qBit are on arr-net, so this hostname resolves. Quote the
              # creds: passwords containing YAML-reserved leading chars
              # (*, &, !, %) would otherwise parse as anchors/aliases.
              type: qbittorrent
              url: http://gluetun:8085
              username: "{{HOMEPAGE_VAR_QBITTORRENT_USER}}"
              password: "{{HOMEPAGE_VAR_QBITTORRENT_PASS}}"
        - Lidarr:
            description: Music
            href: https://lidarr.rosemaryacres.com
            icon: lidarr.png
            widget:
              type: lidarr
              url: http://lidarr:8686
              key: {{HOMEPAGE_VAR_LIDARR_KEY}}
        - LazyLibrarian:
            description: Audiobooks & ebooks
            href: https://lazylibrarian.rosemaryacres.com
            icon: lazylibrarian.png
            # Link-only: Homepage has no LazyLibrarian widget (no API integration).
        - MeTube:
            description: One-off YouTube/video downloads (yt-dlp)
            href: https://metube.rosemaryacres.com
            icon: metube.png
            # Link-only: Homepage has no MeTube widget.
    - AI:
        - Open WebUI:
            description: Local LLM chat + book RAG (Qwen 3B-GPU / 32B-CPU, on Wallace)
            href: https://chat.rosemaryacres.com
            icon: open-webui.png
            # Link-only: the Open WebUI widget needs an API key; not wired here.
        - Open Notebook:
            description: NotebookLM-alt — chat + podcasts over your source docs
            href: https://notebook.rosemaryacres.com
            icon: mdi-notebook-outline
            # Link-only: no Homepage widget for open-notebook.

    # Household — the second half of the Home tab, and the merger of the old
    # "Cloud" and "Productivity" groups. Those two were a distinction without
    # a difference (three tiles and two), and Productivity was one of the two
    # groups missing from settings.layout, so it rendered below everything
    # else. Named for what is actually in it: recipes, documents, books,
    # files, passwords and notes are the household's records, not work output.
    # Uptime Kuma went to Monitoring with the other dashboards, and Stacks
    # came over from Media Mgmt — a book catalogue is not part of the
    # download pipeline.
    - Household:
        # ⚠️ Nextcloud and Tandoor are NOT here — they live in Favorites, which
        # is on the same tab. They were in both at first, which put the only
        # two widget-bearing cards on the Home tab side by side with their own
        # duplicates. Anything promoted to Favorites must be removed from its
        # themed group IF that group shares a tab with Favorites; the Media
        # duplicates are fine precisely because Media sits on another tab.
        - SilverBullet:
            description: Notes, tasks & the daybook (scheduling assistant)
            href: https://notes.rosemaryacres.com
            icon: silverbullet.png
        - Vaultwarden:
            description: Password vault
            href: https://keys.rosemaryacres.com
            icon: vaultwarden.png
        - Paperless:
            description: OCR document archive
            href: https://paperless.rosemaryacres.com
            icon: paperless.png
        - Stacks:
            description: Book catalog — scan at a sale, browse the shelves
            href: https://books.rosemaryacres.com
            icon: mdi-bookshelf
            # Link-only: Homepage has no widget for this; it is ours.

    # Bitcoin / Lightning. mempool has a native Homepage widget (fee + block
    # stats, no key). Alby Hub has no widget, so it's an href tile. bitcoind and
    # Fulcrum have no web UI (RPC / Electrum TCP only), so they get no tile.
    - Bitcoin:
        - mempool:
            description: Blocks, fees & explorer
            href: https://mempool.rosemaryacres.com
            icon: mempool.png
            widget:
              type: mempool
              url: https://mempool.rosemaryacres.com
        - Alby Hub:
            description: Lightning node & Nostr Wallet Connect
            href: https://alby.rosemaryacres.com
            icon: alby.png

    - Infrastructure:
        - Forgejo:
            description: Self-hosted Git (mirrors of GitHub)
            href: https://git.rosemaryacres.com
            icon: forgejo.png
            widget:
              # Forgejo speaks the Gitea API, so the gitea widget works.
              type: gitea
              url: https://git.rosemaryacres.com
              key: {{HOMEPAGE_VAR_FORGEJO_KEY}}
              showOpen: true
              showCounters: true
              showRepoStats: false
        - PinchFlat:
            description: YouTube archiver
            href: https://pinchflat.rosemaryacres.com
            icon: pinchflat.png
        - MeshCentral:
            description: Remote device management (server)
            href: https://mesh.rosemaryacres.com
            icon: meshcentral.png
        - Project board:
            description: Where the agent's multi-day work stands
            href: https://digest.rosemaryacres.com/board/
            icon: mdi-clipboard-text-clock

    - Monitoring:
        - Grafana:
            description: Dashboards & alerting
            href: https://grafana.rosemaryacres.com
            icon: grafana.png
        - Prometheus:
            description: Metrics & queries
            href: https://prometheus.rosemaryacres.com
            icon: prometheus.png
            widget:
              type: prometheus
              # host-gateway, not the vhost: widget fetches are server-side
              # from this container, and the Authelia-protected vhost 302s
              # them to a login page they cannot follow (2026-08 audit M3).
              url: http://host.docker.internal:9090
        - Uptime Kuma:
            description: Status board
            href: https://uptime.rosemaryacres.com
            icon: uptime-kuma.png
            widget:
              type: uptimekuma
              url: https://uptime.rosemaryacres.com
              slug: default

    - Gromit (live):
        # Widget polling goes to the host-gateway ports directly (the
        # br-+ firewall holes in glances.nix/monitoring.nix); the vhosts
        # are Authelia-protected and a server-side fetch cannot log in.
        # Each block below is a separate metric card with a small
        # sparkline. v4 = Glances API v4. Cards (href) link to the full
        # Glances HTML UI, through Authelia like any human visit.
        - CPU:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: http://host.docker.internal:61208
              metric: cpu
              version: 4
              chart: true
              pointsLimit: 30
        - Memory:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: http://host.docker.internal:61208
              metric: memory
              version: 4
              chart: true
              pointsLimit: 30
        - Disk I/O:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: http://host.docker.internal:61208
              metric: disk:nvme0n1
              version: 4
              chart: true
              pointsLimit: 30
        - Network (LAN):
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: http://host.docker.internal:61208
              metric: network:enp3s0
              version: 4
              chart: true
              pointsLimit: 30
        - Load + uptime:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: http://host.docker.internal:61208
              metric: info
              version: 4

    # The temperature graph is a 360px-tall iframe. Left inside "Gromit (live)"
    # it had to share a grid row with the five small metric cards, so it either
    # squeezed into a fifth-width column or stretched that whole row to 360px.
    # Its own full-width group keeps the metric row compact and the graph
    # readable.
    - Temperatures:
        - Gromit temperatures:
            description: CPU / NVMe / ambient °C — warn & critical alerts armed
            href: https://grafana.rosemaryacres.com/d/gromit-temps
            icon: mdi-thermometer
            # `id` lands verbatim on the <li class="service">; customCss sizes
            # the iframe by it. See the ⚠️ below about why not `classes`.
            id: embed-temps
            widget:
              # Same embed pattern as the Riverwatch graph below: panel 1 of
              # the gromit-temps Grafana dashboard via d-solo. Anonymous Viewer
              # + allow_embedding are set in monitoring.nix, so it loads without
              # auth (Grafana is Tailscale-only).
              type: iframe
              name: Temps
              src: https://grafana.rosemaryacres.com/d-solo/gromit-temps/_?orgId=1&panelId=1&theme=dark&from=now-24h&to=now&refresh=1m&kiosk
              # See the embed-river note below: no h-[Npx] here.
              classes: w-full
              referrerPolicy: same-origin
              allowScrolling: "no"

    # The weather strip: current conditions, the latest Ryan Hall read, a
    # one-line summary and a ten-day row with icons. Rendered every 15 minutes
    # by `wx strip` (modules/services/wx) into /var/lib/wx/public and served
    # by nginx as an exact-match location on THIS vhost — same origin as the
    # dashboard, so no second cert, no second DNS record, and no cross-origin
    # restriction on the iframe.
    #
    # ⚠️ Homepage cannot build this card itself. Its openmeteo widget is
    # current-conditions-only, customapi renders text rows with no icons, and
    # nothing in Homepage can reach the wx database for the Ryan Hall line.
    # Hence a rendered page behind an iframe, the same pattern as the
    # Riverwatch and temperature embeds.
    - Weather:
        - Forecast:
            description: Current conditions, the Ryan Hall read, and ten days
            href: https://forecast.weather.gov/
            icon: mdi-weather-partly-cloudy
            id: embed-wx-strip
            widget:
              type: iframe
              name: Weather strip
              src: /wx/strip.html
              # Height in customCss, keyed off `id` — see embed-river.
              classes: w-full
              referrerPolicy: same-origin
              allowScrolling: "no"

    - Riverwatch:
        - Kentucky River — observed + NWPS forecast:
            description: Stage at Lockport & Gratz with flood thresholds
            href: https://grafana.rosemaryacres.com/d/riverwatch
            icon: mdi-chart-line
            id: embed-river
            widget:
              # Embeds panel 1 (Stage time series + flood-threshold reference
              # lines + NWPS 5-day forecast) from the Riverwatch Grafana
              # dashboard. Anonymous Viewer is enabled in monitoring.nix so
              # this loads without auth — fine because Grafana is
              # Tailscale-only. allow_embedding=true is also set there so
              # Grafana doesn't send X-Frame-Options: deny.
              type: iframe
              name: River Graph
              # `to=now+5d` (URL-encoded) makes room for the NWPS forecast
              # overlay, which extends ~5 days into the future.
              src: https://grafana.rosemaryacres.com/d-solo/riverwatch/_?orgId=1&panelId=7&theme=dark&from=now-7d&to=now%2B5d&refresh=5m&kiosk
              # ⚠️ NO h-[Npx] HERE. `classes` REPLACES the widget's default
              # height classes, and an arbitrary Tailwind value is only
              # compiled if it appears in Homepage's own source — ours never
              # is, so the class resolved to nothing and the iframe fell back
              # to the browser's intrinsic 150px. Measured on the live page
              # 2026-10-19: this graph had been rendering at 150px, not 640px.
              # Height is set in customCss via the `id` above.
              classes: w-full
              referrerPolicy: same-origin
              allowScrolling: "no"
        - Lockport (Lock 2):
            description: Kentucky River
            href: https://water.noaa.gov/gauges/lptk2
            icon: mdi-waves
            widget:
              type: customapi
              url: https://api.water.noaa.gov/nwps/v1/gauges/LPTK2
              refreshInterval: 600000
              method: GET
              display: list
              mappings:
                - field:
                    status:
                      observed: primary
                  label: Stage (ft)
                - field:
                    status:
                      observed: secondary
                  label: Flow (kcfs)
                - field:
                    status:
                      forecast: primary
                  label: Forecast crest (ft)
                - field:
                    status:
                      observed: floodCategory
                  label: Status
    - Recent Alerts:
        - ntfy:
            description: Latest message on gromit-alerts
            href: https://ntfy.rosemaryacres.com
            icon: ntfy.png
            widget:
              type: ntfy
              url: https://ntfy.rosemaryacres.com
              topic: gromit-alerts
              # Anonymous subscribe is denied since 2026-08-19 (see
              # notifications.nix). Credentials come from an env file that
              # ntfy-provision generates outside the nix store.
              username: {{HOMEPAGE_VAR_NTFY_USER}}
              password: {{HOMEPAGE_VAR_NTFY_PASS}}
  '';

  bookmarksYaml = pkgs.writeText "homepage-bookmarks.yaml" ''
    - Code:
        - GitHub:
            - abbr: GH
              href: https://github.com/ww4
        - flakes repo:
            - abbr: FL
              href: https://github.com/ww4/flakes
    - Admin:
        - Cloudflare:
            - abbr: CF
              href: https://dash.cloudflare.com
        - Tailscale:
            - abbr: TS
              href: https://login.tailscale.com/admin/machines
  '';

  widgetsYaml = pkgs.writeText "homepage-widgets.yaml" ''
    - resources:
        label: gromit
        cpu: true
        memory: true
        disk:
          - /
          - /mnt/fusion
          - /mnt/backup/all
    - search:
        provider: duckduckgo
        target: _blank
    - datetime:
        text_size: xl
        format:
          # medium ("Oct 19, 2026") rather than long ("October 19, 2026") —
          # the long form wraps the header on a phone for no added meaning.
          dateStyle: medium
          timeStyle: short
          hour12: true
  '';

  # Required by Homepage but unused — empty avoids "file not found" warnings.
  dockerYaml     = pkgs.writeText "homepage-docker.yaml" "";
  kubernetesYaml = pkgs.writeText "homepage-kubernetes.yaml" "";

in {
  # Homepage widget API keys via sops (migrated 2026-06-16). Read by root (docker
  # --env-file) before the container starts → root:0400.
  sops.secrets."homepage-env" = {
    sopsFile = ../../secrets/homepage-env.yaml;
    key = "homepage-env";
  };

  virtualisation.oci-containers = {
    backend = "docker";
    containers.homepage = {
      image = "ghcr.io/gethomepage/homepage:latest@sha256:638dacf5c844e908dc06c1fd57a2b5694f8efd91f91f152829ea0c2f547458f2";
      ports = [ "127.0.0.1:${toString port}:3000" ];
      volumes = [
        "${settingsYaml}:/app/config/settings.yaml:ro"
        "${servicesYaml}:/app/config/services.yaml:ro"
        "${bookmarksYaml}:/app/config/bookmarks.yaml:ro"
        "${widgetsYaml}:/app/config/widgets.yaml:ro"
        "${customCss}:/app/config/custom.css:ro"
        "${dockerYaml}:/app/config/docker.yaml:ro"
        "${kubernetesYaml}:/app/config/kubernetes.yaml:ro"
        # Mounted so the resources widget can statfs() these — host paths
        # don't exist inside the container otherwise.
        "/mnt/fusion:/mnt/fusion:ro"
        "/mnt/backup/all:/mnt/backup/all:ro"
      ];
      environmentFiles = [
        config.sops.secrets."homepage-env".path
        # ntfy subscriber credentials, generated by ntfy-provision (not sops,
        # not the nix store). Created empty by tmpfiles so a first boot cannot
        # fail on a missing file.
        "/var/lib/ntfy-sh/homepage-ntfy.env"
      ];
      environment = {
        HOMEPAGE_ALLOWED_HOSTS = "${hostname},www.${hostname}";
      };
      # Join arr-net (created by modules/services/arr.nix) so the *arr stack
      # widgets can resolve `prowlarr`, `sonarr`, `gluetun`, etc. by name.
      extraOptions = [
        "--network=arr-net"
        # host.docker.internal -> the container's bridge gateway, so widget
        # urls can name the host without hardcoding a docker subnet.
        "--add-host=host.docker.internal:host-gateway"
      ];
    };
  };

  # The container reads its secrets from sops (environmentFiles above);
  # /var/lib/homepage/secrets.env was the pre-sops home and is no longer
  # read — the tmpfiles rule that kept creating it is gone (2026-08 audit).
  systemd.tmpfiles.rules = [
    "d /var/lib/homepage 0755 root root - -"
  ];

  # Apex + www on Cloudflare DNS-01; www → apex 301.
  services.nginx.virtualHosts."${hostname}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      proxyWebsockets = true;
    };

    # The weather strip, rendered by `wx strip` (modules/services/wx) and
    # iframed by the Weather tab. Served from this vhost rather than its own
    # so the iframe is SAME-ORIGIN — no second certificate, no second DNS
    # record, and no X-Frame-Options/CSP negotiation with ourselves.
    #
    # ⚠️ An exact-match (`= /path`) location beats the `/` prefix location
    # above, so this does NOT get proxied into the Homepage container. A
    # prefix location here would lose to nothing but is easy to get wrong —
    # the same trap as the MIME/regex ordering noted in nginx-config-verify.
    locations."= /briefing/card.html" = {
      alias = "/var/lib/briefing/public/card.html";
      extraConfig = ''
        # Regenerated every five minutes; a cached copy would show a resolved
        # issue as still open.
        add_header Cache-Control "no-cache, must-revalidate";
      '';
    };

    locations."= /wx/strip.html" = {
      alias = "/var/lib/wx/public/strip.html";
      extraConfig = ''
        # Regenerated every 15 minutes; a stale cached copy would quietly
        # show yesterday's forecast, which is worse than a slow load.
        add_header Cache-Control "no-cache, must-revalidate";
      '';
    };
  };

  services.nginx.virtualHosts."www.${hostname}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    globalRedirect = hostname;
  };
}
