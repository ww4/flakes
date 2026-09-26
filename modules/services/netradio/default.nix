# Net radio for the Yamaha R-N301 — the receiver's "Net Radio" input, brought
# back after vTuner went pay-to-use, plus radio stations made from the music
# library.
#
# How the receiver finds radio at all: it looks up radioyamaha.vtuner.com,
# speaks plain HTTP on port 80 to whatever answers, and plays the stream URLs
# it is handed. So:
#
#   receiver ──DNS──▶ Blocky: radioyamaha.vtuner.com = this box (LAN IP)
#            ──:80──▶ nginx vhost radioyamaha.vtuner.com
#                       /            → YCast (vTuner emulation, 127.0.0.1:8010)
#                                      • Radiobrowser: the community index
#                                        (genres / countries / popular)
#                                      • My Stations: the library stations
#                                        below + a few quick picks
#                       /radio/X.mp3 → auth_request → netradio-wake (:8011)
#                                      starts Liquidsoap output X, waits for
#                                      Icecast to list it, then nginx proxies
#                                      the listener to Icecast (127.0.0.1:8020)
#
# Library stations are genre-tag playlists, era-filtered by the profiler's
# audio-quality buckets (netradio-playlists, nightly), fed
# to Icecast by Liquidsoap, sequenced by a DJ (netradio-dj) that queues the
# tracks and every 3-4 of them a Kokoro-voiced break naming what just played
# and what is next. A profiler (netradio-profile, nightly) listens to each
# track once so spoken intros/stage talk cut as their own tracks stay off the
# stations, and songs that start or end in chatter aren't crossfaded over.
# Encoders are ON DEMAND: a station's MP3 encoder
# runs only while someone is listening (+5 min grace), so nine stations idle
# at zero CPU. Nothing here needs a port opened — the receiver only ever
# touches port 80, which is already open and LAN/Tailscale-gated by
# nginx-access.nix. Icecast, YCast and the wake server are loopback-only.
#
# Known limit: the receiver cannot do TLS, and YCast rewrites https:// stream
# URLs to http://. A station that serves HTTPS only will fail to play; the
# Radiobrowser index still has plenty that don't.
#
# Stations, specialty feeds and the schedule are RUNTIME config
# (${stateDir}/config/, seeded once from this file) edited on
# radio.rosemaryacres.com/admin. Two kinds of station: CURATED (a broad base
# rule plus a schedule of segments — themed hours drawn from specialty feeds
# or artist spotlights with similar artists mixed in; `auto` slots are the
# DJ's own pick for the day) and SPECIALTY (one feed, listenable on its own).
# A new feed's rule is written by `claude -p` from its description (the
# apply path unit runs it as the claude user); the DJ announces segments as
# they start and the day's schedule at breaks, radio-style.
#
# The page at radio.rosemaryacres.com (web/) shows what's playing, the last
# few played, what the DJ queued next, listener counts and a visualiser, and
# serves stations.m3u / .pls for radio apps. Every station also has a
# "-lo" mount at 96 kbps (cellular). Nothing here needs a port opened.
#
# Ops:
#   sudo cat /var/lib/netradio/credentials.env      Icecast passwords (generated)
#   radio.rosemaryacres.com/admin                   feeds, schedule, stations (the config)
#   /var/lib/netradio/config/                       feeds.json stations.json schedule.json picks.json
#   journalctl -u netradio-apply                    what the admin's requests did (rescan / restart / compile)
#   systemctl start netradio-playlists              rescan the library now
#   systemctl start netradio-profile                profile new tracks now (first run: hours)
#   /var/lib/netradio/profile/profile-report.txt    what the profiler flagged; fix in profile-overrides.json beside it
#   journalctl -u netradio-wake                     which station started/stopped
#   http://127.0.0.1:8020/status.xsl                what Icecast is serving
{ config, lib, pkgs, ... }:

let
  ycast = pkgs.callPackage ../../../pkgs/ycast { };
  netradio = pkgs.callPackage ./package.nix { };

  # The vTuner names the receiver resolves. Both point here.
  vtunerHost = "radioyamaha.vtuner.com";
  vtunerBackup = "radioyamaha2.vtuner.com";
  # The LAN address nginx is on — the same answer Blocky gives for
  # *.rosemaryacres.com (proxyIP in blocky.nix; reading it back from
  # config.services.blocky.settings while also adding to it is an infinite
  # recursion, so it is repeated here — keep the two together).
  lanIP = "192.168.1.65";

  ycastPort = 8010;
  wakePort = 8011;
  adminPort = 8012;
  speakerPort = 8013;
  # gromit's analog out (Realtek ALC887-VD, card 0). The rain starts at boot
  # but MUTED, like the receiver: the stream is running and the jack is silent
  # until the remote's mute button says otherwise.
  speakerCard = "0";
  speakerDefaultMount = "rainymood";   # the single seamless loop, not the five-bed variety station
  speakerStartVolume = 35;
  icecastPort = 8020; # 8000 is audiobookshelf (icecast SEGVs when the bind fails)

  user = "netradio";
  stateDir = "/var/lib/netradio";
  credsEnv = "${stateDir}/credentials.env";
  playlistDir = "${stateDir}/playlists";
  cacheDir = "${stateDir}/cache";
  tagCache = "${cacheDir}/tags.json";
  djDir = "${stateDir}/dj";
  nowDir = "${stateDir}/now";   # what's playing / last played / up next, per station — the page reads it via nginx
  # Its own subdir, like playlists/ and dj/: ${stateDir} itself is root-owned
  # (it holds credentials.env), so the netradio user cannot create the
  # profile.json.tmp the atomic save needs there (2026-09-16 first-run failure).
  ambientDir = "${stateDir}/ambient";   # rain and the like: the fixed stations' beds, never in the music library
  profileDir = "${stateDir}/profile";
  profileJson = "${profileDir}/profile.json";
  profileOverrides = "${profileDir}/profile-overrides.json";   # {path: "talk"|"music"}, hand-edited
  profileReport = "${profileDir}/profile-report.txt";

  yamnet = pkgs.callPackage ./yamnet.nix { };

  # The profiling itself is offloaded to wallace when it is up (the 5900X does
  # it 4-5x faster; hosts/wallace/netradio-profile-server.nix), same shape as
  # the switchboard's whisper/Kokoro: gromit's workers try it first and
  # measure locally when nobody answers. Eight client workers keep twelve
  # server processes fed over the tailnet (LAN-direct); if wallace is off the
  # eight fall back to this box's four cores — slower, still correct.
  profileRemotes = [ "http://100.66.171.120:8790" ];
  profileWorkers = 8;

  runDir = "/run/netradio";
  liqSocket = "${runDir}/liquidsoap.sock";

  # The DJ's voice: the switchboard's Kokoro choice and URL order (wallace
  # first, gromit's own container last) so the house has one voice.
  kokoroVoice = config.services.switchboard.kokoroVoice;
  kokoroUrls = config.services.switchboard.remoteKokoroUrls ++ [ "http://127.0.0.1:8880" ];

  # Where the music is. The first is the Jellyfin library; the second is
  # where Lidarr puts new albums (lidarr.nix).
  libraryRoots = [ "/mnt/fusion/Music" "/mnt/fusion/arr/media/music" ];

  # --- the runtime config and its seeds ---------------------------------------
  # Stations, specialty feeds and the schedule are RUNTIME config under
  # ${stateDir}/config/, edited on radio.rosemaryacres.com/admin; the scanner,
  # the DJ, Liquidsoap (script rendered at start), YCast (menu file) and the
  # wake service all read it. These seeds are copied in ONCE, on a box that
  # has no config yet — Chris's edits are never overwritten by a deploy.
  #
  # A feed's rule (feeds.py): artists / genres / instruments (YAMNet means)
  # / era, clauses AND-ed. A station's base is the same shape. Families are
  # the compatibility vocabulary (config.py): the admin page offers a
  # station only feeds and artists whose families overlap its own.
  configDir = "${stateDir}/config";
  poolsDir = "${stateDir}/pools";

  noShellac = { exclude = [ "shellac" ]; };
  seedFeeds = {
    brother-duets = {
      shellac = true;   # 1930s-40s sides are the point of this feed
      title = "Brother Duets";
      description = "Close-harmony duets by brothers and brotherly pairs: the Louvins, Delmores, Blue Sky Boys, Stanleys, Monroe Brothers, Lilly Brothers, Whitsteins, Bailes Brothers, Osbornes.";
      family = [ "bluegrass" "country" ]; status = "ready"; count = 0;
      note = "seeded from the library's artist list";
      rule = { artists = [ "Stanley Brothers" "Louvin Brothers" "Blue Sky Boys" "Delmore Brothers" "Lilly Brothers" "Whitstein Brothers"
                           "Bailes Brothers" "Osborne Brothers" "Bill and Charlie Monroe" "Monroe Brothers" "Jim & Jesse"
                           "Bobby Osborne And Jesse McReynolds" "Jesse McReynolds & Charles Whitstein" "Everly Brothers" ];
               genres = [ "brother duets" ]; };
    };
    western-swing = {
      shellac = true;   # 1930s-40s sides are the point of this feed
      title = "Western Swing";
      description = "Dance-hall country with jazz in it: Bob Wills and the Texas Playboys, Milton Brown, Spade Cooley, and the revivalists — Asleep at the Wheel, Hot Club of Cowtown.";
      family = [ "country" ]; status = "ready"; count = 0; note = "seeded; thin until the metadata pass adds Last.fm genres";
      rule = { artists = [ "Bob Wills" "Milton Brown" "Spade Cooley" "Asleep At The Wheel" "Hot Club of Cowtown" "Light Crust Doughboys" "Tex Williams" "Hank Thompson" ];
               genres = [ "western swing" ]; };
    };
    honky-tonk = {
      shellac = true;   # 1930s-40s sides are the point of this feed
      title = "Honky Tonk";
      description = "Barroom country of the 1950s and 60s and its keepers: Hank Williams, Lefty Frizzell, Ernest Tubb, Webb Pierce, Ray Price, George Jones, Faron Young, Johnny Bush, Gary Stewart, Moe Bandy.";
      family = [ "country" ]; status = "ready"; count = 0; note = "seeded from the library's artist list";
      rule = { artists = [ "Hank Williams" "Lefty Frizzell" "Ernest Tubb" "Webb Pierce" "Ray Price" "George Jones" "Faron Young"
                           "Johnny Bush" "Gary Stewart" "Moe Bandy" "Hank Thompson" "Johnny Paycheck" "Vern Gosdin" ];
               genres = [ "honky tonk" ]; };
    };
    old-time-fiddle = {
      title = "Old-Time Fiddle";
      description = "Fiddle-led old-time tunes: Tommy Jarrell, Art Stamper, Benton Flippen, Bruce Molsky, Clyde Davenport — the tune, not the song.";
      family = [ "bluegrass" "folk" ]; status = "ready"; count = 0; note = "seeded: old-time tag AND a fiddle in the mix";
      rule = { genres = [ "old time" "oldtime" "fiddle" ]; artists = [ "Tommy Jarrell" "Art Stamper" "Benton Flippen" "Bruce Molsky" "Clyde Davenport" ];
               instruments = { violin = [ 0.15 null ]; }; };
    };
    banjo-instrumentals = {
      title = "Banjo Instrumentals";
      description = "Banjo out front and nobody singing: bluegrass and clawhammer instrumentals.";
      family = [ "bluegrass" "folk" ]; status = "ready"; count = 0; note = "seeded from the audio measurements";
      rule = { instruments = { banjo = [ 0.3 null ]; singing = [ null 0.05 ]; }; };
    };
    a-cappella = {
      title = "A Cappella & Lined-Out Singing";
      description = "Unaccompanied singing: Old Regular Baptist lined-out hymnody, ballad singers, shape-note and quartet singing without instruments.";
      family = [ "gospel" "folk" ]; status = "ready"; count = 0; note = "seeded from the audio measurements";
      rule = { instruments = { a_capella = [ 0.15 null ]; }; };
    };
    cajun-zydeco = {
      title = "Cajun & Zydeco";
      description = "Louisiana dance music: Cajun two-steps and waltzes, zydeco.";
      family = [ "folk" "country" ]; status = "ready"; count = 0; note = "seeded from the genre tags";
      rule = { genres = [ "cajun" "zydeco" ]; };
    };
  };
  specialtyStation = id: f: { mount = id; name = f.title; kind = "specialty"; feed = id; inherit (f) family; };
  seedStations = [
    { mount = "all";        name = "Everything";           kind = "curated"; family = [ "any" ];                base = { all = true; }; }
    { mount = "scratchy";   name = "Old Scratchy Records"; kind = "curated"; family = [ "any" ];                base = { all = true; era = { only = [ "shellac" ]; }; }; }
    { mount = "bluegrass";  name = "Bluegrass & Old-Time"; kind = "curated"; family = [ "bluegrass" "folk" ];   base = { genres = [ "bluegrass" "old time" "oldtime" "newgrass" "string band" "brother duets" "appalachian" ]; era = noShellac; }; }
    { mount = "country";    name = "Classic Country";      kind = "curated"; family = [ "country" ];            base = { genres = [ "country" "western swing" "honky tonk" "western" "cowboy" ]; exclude_genres = [ "bluegrass" "old time" "oldtime" "newgrass" "string band" "brother duets" "appalachian" ]; era = noShellac; }; }
    { mount = "folk";       name = "Folk";                 kind = "curated"; family = [ "folk" "bluegrass" ];   base = { genres = [ "folk" "singer songwriter" "celtic" "traditional" "americana" "acoustic" "irish" "cajun" "zydeco" ]; era = noShellac; }; }
    { mount = "rock";       name = "Rock";                 kind = "curated"; family = [ "rock" ];               base = { genres = [ "rock" "alternative" "pop" "punk" "metal" "indie" "new wave" ]; era = noShellac; }; }
    { mount = "blues";      name = "Blues";                kind = "curated"; family = [ "blues-jazz" ];         base = { genres = [ "blues" ]; era = noShellac; }; }
    { mount = "jazz";       name = "Jazz";                 kind = "curated"; family = [ "blues-jazz" ];         base = { genres = [ "jazz" "big band" "swing" "fusion" "bebop" "dixieland" ]; era = noShellac; }; }
    { mount = "soul";       name = "Soul & R&B";           kind = "curated"; family = [ "blues-jazz" ];         base = { genres = [ "soul and r&b" "soul" "r&b" "funk" "motown" ]; era = noShellac; }; }
    { mount = "gospel";     name = "Gospel";               kind = "curated"; family = [ "gospel" ];             base = { genres = [ "gospel" "religious" "christian" "hymns" "sacred" "spiritual" ]; }; }
    { mount = "classical";  name = "Classical";            kind = "curated"; family = [ "classical" ];          base = { genres = [ "classical" "baroque" "orchestral" "opera" "chamber" ]; }; }
    { mount = "holiday";    name = "Holiday";              kind = "curated"; family = [ "holiday" ];            base = { genres = [ "holiday" "christmas" "xmas" ]; }; }
  ] ++ lib.mapAttrsToList specialtyStation seedFeeds;
  seedSchedule = [
    { id = "country-sat-swing";  station = "country";   name = "Western Swing Hour";     kind = "feed"; feed = "western-swing";   days = [ "sat" ]; start = "10:00"; minutes = 60; }
    { id = "country-happy-hour"; station = "country";   name = "Honky Tonk Happy Hour";  kind = "feed"; feed = "honky-tonk";      days = "weekdays"; start = "17:00"; minutes = 60; }
    { id = "country-spotlight";  station = "country";   kind = "auto"; like = "artist";  days = "daily"; start = "20:00"; minutes = 60; }
    { id = "bluegrass-duets";    station = "bluegrass"; name = "Brother Duets Hour";     kind = "feed"; feed = "brother-duets";   days = [ "sun" ]; start = "09:00"; minutes = 60; }
    { id = "bluegrass-banjo";    station = "bluegrass"; name = "Banjo Hour";             kind = "feed"; feed = "banjo-instrumentals"; days = [ "wed" ]; start = "19:00"; minutes = 60; }
    { id = "folk-fiddle";        station = "folk";      name = "Old-Time Fiddle Hour";   kind = "feed"; feed = "old-time-fiddle"; days = [ "sat" ]; start = "09:00"; minutes = 60; }
  ];
  seedFile = name: data: pkgs.writeText "netradio-seed-${name}" (builtins.toJSON data);
  seeds = { feeds = seedFile "feeds.json" seedFeeds; stations = seedFile "stations.json" seedStations; schedule = seedFile "schedule.json" seedSchedule; };

  # A few known-good plain-HTTP streams so the input has something to play
  # before any browsing (all verified 2026-09-15). Discovery is Radiobrowser's
  # job; this is not a curated list.
  quickPicks = [
    { name = "NPR News";            url = "http://npr-ice.streamguys1.com/live.mp3"; }
    { name = "WFPK Louisville";     url = "http://lpm.streamguys1.com/wfpk-web"; }
    { name = "WUKY Lexington";      url = "http://wuky.streamguys1.com/wuky"; }
    { name = "Radio Paradise";      url = "http://stream.radioparadise.com/mp3-128"; }
    { name = "KEXP Seattle";        url = "http://kexp-mp3-128.streamguys1.com/kexp128.mp3"; }
    { name = "WWOZ New Orleans";    url = "http://wwoz-sc.streamguys1.com/wwoz-hi.mp3"; }
    { name = "WQXR Classical";      url = "http://stream.wqxr.org/wqxr"; }
    { name = "SomaFM Folk Forward"; url = "http://ice1.somafm.com/folkfwd-128-mp3"; }
    { name = "SomaFM Boot Liquor";  url = "http://ice1.somafm.com/bootliquor-128-mp3"; }
    { name = "SomaFM Groove Salad"; url = "http://ice1.somafm.com/groovesalad-128-mp3"; }
    { name = "SomaFM Secret Agent"; url = "http://ice1.somafm.com/secretagent-128-mp3"; }
  ];
  quickPicksJson = pkgs.writeText "netradio-quick-picks.json" (builtins.toJSON quickPicks);
  # A stations.yml with only the Quick Picks, for YCast to start on before
  # the scanner has written the real one (JSON is valid YAML).
  ycastSeedYaml = pkgs.writeText "netradio-stations-seed.yml"
    (builtins.toJSON { "Quick Picks" = builtins.listToAttrs (map (p: { name = p.name; value = p.url; }) quickPicks); });

  radioHost = "radio.rosemaryacres.com";

  # Library streams, shared by both vhosts. auth_request wakes the encoder
  # first; then the listener is proxied straight to Icecast, unbuffered, with
  # the ICY metadata headers passing both ways so the display shows the track.
  streamLocations = {
    "/radio/" = {
      proxyPass = "http://127.0.0.1:${toString icecastPort}/";
      extraConfig = ''
        auth_request /_wake;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        proxy_set_header Icy-MetaData $http_icy_metadata;
      '';
    };
    "= /_wake" = {
      extraConfig = ''
        internal;
        proxy_pass http://127.0.0.1:${toString wakePort}/wake;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URI $request_uri;
        proxy_read_timeout 20s;
      '';
    };
  };

  # The radio page and the admin page (web/): Vue 3 (global build, no
  # bundler) on Pico CSS, both vendored here by hash — the vhost is
  # Tailscale-only, so nothing loads from a CDN at runtime. Everything
  # station-shaped the pages need is RUNTIME data (the scanner's now/ files
  # and the admin API); nothing here depends on the station list.
  vueJs = pkgs.fetchurl {
    url = "https://cdn.jsdelivr.net/npm/vue@3.5.13/dist/vue.global.prod.js";
    hash = "sha256-xFm6fMjbZcmCWJ+l1kx/9HiHfo5bD9dWgyB87GpOieg=";
  };
  picoCss = pkgs.fetchurl {
    url = "https://cdn.jsdelivr.net/npm/@picocss/pico@2.0.6/css/pico.min.css";
    hash = "sha256-3V/VWRr9ge4h3MEXrYXAFNw/HxncLXt9EB6grMKSdMI=";
  };
  radioWeb = pkgs.runCommand "netradio-web" { } ''
    mkdir -p $out/admin $out/vendor
    cp ${./web}/index.html ${./web}/app.js ${./web}/remote.css ${./web}/ui.css $out/
    cp ${./web}/desktop.html ${./web}/desktop.js $out/   # the wide-screen page (the remote redirects there)
    cp ${./web}/manifest.webmanifest ${./web}/sw.js ${./web}/icon.svg ${./web}/icon-192.png ${./web}/icon-512.png $out/   # the PWA
    cp ${./web/admin}/index.html ${./web/admin}/admin.js $out/admin/
    cp ${vueJs} $out/vendor/vue.global.prod.js
    cp ${picoCss} $out/vendor/pico.min.css
  '';

  # YCast's menu (config/stations.yml) and the Liquidsoap script
  # (/run/netradio/netradio.liq) are rendered from the runtime station list:
  # the scanner writes the first, `netradio liq` the second at service start.
  liqScript = "${runDir}/netradio.liq";

  # Icecast config, rendered at start with the generated passwords. The
  # nixpkgs module takes the password as a Nix string, which would put it in
  # the store; this keeps it in the root-only env file.
  icecastTemplate = pkgs.writeText "icecast.xml.in" ''
    <?xml version="1.0"?>
    <icecast>
      <location>gromit</location>
      <admin>root@localhost</admin>
      <hostname>127.0.0.1</hostname>
      <limits>
        <clients>32</clients>
        <sources>64</sources>   <!-- two encodes per station; the list is runtime config -->
        <queue-size>524288</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-size>65535</burst-size>
      </limits>
      <authentication>
        <source-password>@ICECAST_SOURCE_PASSWORD@</source-password>
        <relay-password>@ICECAST_SOURCE_PASSWORD@</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>@ICECAST_ADMIN_PASSWORD@</admin-password>
      </authentication>
      <listen-socket>
        <port>${toString icecastPort}</port>
        <bind-address>127.0.0.1</bind-address>
      </listen-socket>
      <paths>
        <logdir>/var/log/icecast</logdir>
        <webroot>${pkgs.icecast}/share/icecast/web</webroot>
        <adminroot>${pkgs.icecast}/share/icecast/admin</adminroot>
        <alias source="/" dest="/status.xsl"/>
        <mime-types>${pkgs.mailcap}/etc/mime.types</mime-types>
      </paths>
      <logging>
        <accesslog>access.log</accesslog>
        <errorlog>error.log</errorlog>
        <loglevel>2</loglevel>
        <logsize>10000</logsize>
      </logging>
      <security>
        <chroot>0</chroot>
      </security>
    </icecast>
  '';

  hardening = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
  };
in
{
  users.users.${user} = {
    isSystemUser = true;
    group = user;
    extraGroups = [ "media" ];  # the library is jellyfin:media 0770/0774
    home = stateDir;
  };
  users.groups.${user} = { };

  # --- DNS: the receiver's lookups land here -------------------------------
  # LAN-only by construction (blocky.nix): a Tailscale client keeps public DNS
  # and gets the real vTuner, which is fine — nothing off-LAN uses this.
  services.blocky.settings.customDNS.mapping = {
    ${vtunerHost} = lanIP;
    ${vtunerBackup} = lanIP;
  };

  # --- nginx: the ONE thing the receiver talks to ---------------------------
  # Plain HTTP on 80, deliberately no forceSSL: the receiver has no TLS.
  services.nginx.virtualHosts.${vtunerHost} = {
    serverAliases = [ vtunerBackup ];
    extraConfig = ''
      # A 2014 receiver's HTTP parser: no compressed bodies, no chunking.
      gzip off;
      chunked_transfer_encoding off;
    '';

    # vTuner API → YCast. YCast builds the URLs it hands back from the Host
    # header, so it must see radioyamaha.vtuner.com (recommendedProxySettings
    # passes Host through).
    locations = {
      "/" = {
        proxyPass = "http://127.0.0.1:${toString ycastPort}";
        recommendedProxySettings = true;
      };
    } // streamLocations;
  };

  # --- radio.rosemaryacres.com: the same streams for phones and laptops -----
  # Tailscale-reachable (A record → the Tailscale IP, DNS-01 cert) and
  # source-gated like every other vhost. Exists because the LAN cannot be
  # steered to Blocky (the Askey router ignores a LAN forwarder), so the
  # vtuner name only resolves here for devices given .65 as DNS by hand; this
  # name resolves everywhere. Index page: the stations, tap to play.
  services.nginx.virtualHosts.${radioHost} = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    root = radioWeb;
    locations = {
      "/" = {
        tryFiles = "$uri $uri/index.html =404";
      };
      # Per-station "last played" (Liquidsoap) and "up next" (the DJ), the
      # scanner's counts, and the radio-app playlists — plain files,
      # rewritten atomically. `^~` so no regex location can take these URIs
      # away; the playlist MIME types are a NESTED location, because a
      # `types` block REPLACES the inherited map for its location (put in
      # "/" it served the page as octet-stream, 2026-09-16 11:37) and a
      # top-level regex location outran this prefix and 404'd the m3u
      # files from the site root (17:25 the same day).
      "^~ /now/" = {
        root = stateDir;
        extraConfig = ''
          add_header Cache-Control "no-store";
          location ~ \.(m3u|pls)$ {
            types { audio/x-mpegurl m3u; audio/x-scpls pls; }
            add_header Cache-Control "no-store";
          }
        '';
      };
      # nginx's mime.types has no entry for .webmanifest, and Chrome won't
      # install a PWA whose manifest arrives as octet-stream. An exact-match
      # location for that one file (a `types` block only reaches this file).
      "= /manifest.webmanifest" = {
        extraConfig = ''
          types { } default_type application/manifest+json;
        '';
      };
      # The receiver's JSON API (yamaha-ync-api, loopback; modules/services/yamaha-ync.nix)
      # for the page's living-room controls. Same Tailscale/LAN gate as the page.
      "/receiver/" = {
        proxyPass = "http://127.0.0.1:${toString config.services.yamaha-ync.apiPort}/";
        extraConfig = ''
          add_header Cache-Control "no-store";
          proxy_read_timeout 90s;   # a menu walk can take a while
        '';
      };
      # gromit's own sound card as a third endpoint (netradio-speaker)
      "/speaker/" = {
        proxyPass = "http://127.0.0.1:${toString speakerPort}/";
        extraConfig = ''
          add_header Cache-Control "no-store";
        '';
      };
      # The admin API (netradio admin, loopback). The page itself is static
      # under /admin/. The vhost's Tailscale/LAN gate is the perimeter.
      "/admin/api/" = {
        proxyPass = "http://127.0.0.1:${toString adminPort}/api/";
        extraConfig = ''
          add_header Cache-Control "no-store";
        '';
      };
      # Icecast's public status: which mounts are up, titles, listener counts.
      "= /icecast-status" = {
        proxyPass = "http://127.0.0.1:${toString icecastPort}/status-json.xsl";
        extraConfig = ''
          add_header Cache-Control "no-store";
        '';
      };
    } // streamLocations;
  };

  # --- Icecast passwords: generated on the box, never in the repo ------------
  # Read them with `sudo cat ${credsEnv}`; rotate by deleting the file and
  # restarting netradio-credentials netradio-icecast netradio-liquidsoap.
  # Same temp-file-then-move shape as homelab-mcp-credentials, for the same
  # reason (a half-written file must not satisfy the `! -s` guard next time).
  systemd.services.netradio-credentials = {
    description = "Generate the Icecast source/admin passwords if absent";
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -euo pipefail
      install -d -m 0755 -o root -g root ${stateDir}
      install -d -m 0755 -o ${user} -g ${user} ${playlistDir}
      install -d -m 0700 -o ${user} -g ${user} ${cacheDir}
      install -d -m 0755 -o ${user} -g ${user} ${djDir} ${djDir}/inbox   # inbox: skip/request files from the admin API
      install -d -m 0755 -o ${user} -g ${user} ${nowDir}
      install -d -m 0755 -o ${user} -g ${user} ${profileDir}
      install -d -m 0755 -o ${user} -g ${user} ${ambientDir} ${ambientDir}/rain
      install -d -m 0755 -o ${user} -g ${user} ${poolsDir} ${poolsDir}/feeds ${poolsDir}/artists
      # The runtime config: netradio writes it (admin, scanner, DJ) and the
      # claude user's compile job writes feeds.json too, so it is group
      # `users` and setgid. Seeds land only where no file exists.
      install -d -m 2775 -o ${user} -g users ${configDir} ${configDir}/requests
      [ -s ${configDir}/feeds.json ]    || install -m 0664 -o ${user} -g users ${seeds.feeds} ${configDir}/feeds.json
      [ -s ${configDir}/stations.json ] || install -m 0664 -o ${user} -g users ${seeds.stations} ${configDir}/stations.json
      [ -s ${configDir}/schedule.json ] || install -m 0664 -o ${user} -g users ${seeds.schedule} ${configDir}/schedule.json
      # One-time changes that should reach an EXISTING config too (a seed
      # change only reaches a fresh install): netradio/migrate.py, recorded
      # in config/migrations.json. Runs as the config's owner.
      ${pkgs.util-linux}/bin/runuser -u ${user} -- ${netradio}/bin/netradio migrate --config ${configDir}
      # Liquidsoap watches each playlist FILE (inotify): one that appears
      # after it started is never picked up, but an empty one that is later
      # rewritten is (verified 2026-09-15). So every station's file exists
      # before the first start; the scanner fills them.
      for m in $(${pkgs.jq}/bin/jq -r '.[].mount' ${configDir}/stations.json); do
        f=${playlistDir}/$m.m3u
        [ -e "$f" ] || { printf '#EXTM3U\n' > "$f"; chown ${user}:${user} "$f"; }
      done
      if [ ! -s ${credsEnv} ]; then
        umask 077
        tmp="$(${pkgs.coreutils}/bin/mktemp ${stateDir}/.credentials.XXXXXX)"
        trap 'rm -f "$tmp"' EXIT
        {
          printf 'ICECAST_SOURCE_PASSWORD=%s\n' "$(${pkgs.openssl}/bin/openssl rand -hex 16)"
          printf 'ICECAST_ADMIN_PASSWORD=%s\n'  "$(${pkgs.openssl}/bin/openssl rand -hex 16)"
        } > "$tmp"
        chown root:root "$tmp"
        chmod 0600 "$tmp"
        mv "$tmp" ${credsEnv}
        trap - EXIT
        echo "generated new icecast credentials"
      fi
    '';
  };

  # --- Icecast ---------------------------------------------------------------
  systemd.services.netradio-icecast = {
    description = "Icecast (loopback) for the library radio stations";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" "netradio-credentials.service" ];
    requires = [ "netradio-credentials.service" ];
    serviceConfig = hardening // {
      DynamicUser = true;
      EnvironmentFile = credsEnv;
      RuntimeDirectory = "netradio-icecast";
      LogsDirectory = "icecast";
      # The env file is read by PID 1, so ExecStartPre sees the passwords
      # without the unit's user being able to open the file itself.
      ExecStartPre = pkgs.writeShellScript "render-icecast-xml" ''
        set -eu
        umask 077
        ${pkgs.gnused}/bin/sed \
          -e "s|@ICECAST_SOURCE_PASSWORD@|$ICECAST_SOURCE_PASSWORD|" \
          -e "s|@ICECAST_ADMIN_PASSWORD@|$ICECAST_ADMIN_PASSWORD|" \
          ${icecastTemplate} > /run/netradio-icecast/icecast.xml
      '';
      ExecStart = "${lib.getExe pkgs.icecast} -c /run/netradio-icecast/icecast.xml";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };

  # --- Liquidsoap: the encoders (idle until woken) ---------------------------
  systemd.services.netradio-liquidsoap = {
    description = "Liquidsoap — library radio station outputs";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-icecast.service" "netradio-credentials.service" "mnt-fusion.mount" ];
    requires = [ "netradio-icecast.service" "netradio-credentials.service" ];
    # Liquidsoap restarts whenever the station list changes (its script
    # does); pull a playlist rebuild along so new stations are populated.
    # Not `requires`: a scan failure must not take the radio down.
    wants = [ "netradio-playlists.service" ];
    environment.HOME = stateDir;
    serviceConfig = hardening // {
      User = user;
      Group = user;
      SupplementaryGroups = [ "media" ];
      EnvironmentFile = credsEnv;
      RuntimeDirectory = "netradio";
      RuntimeDirectoryMode = "0700";
      ReadWritePaths = [ nowDir ];
      # The script is rendered from the runtime station list every start;
      # a station added on the admin page is one restart away (the apply
      # path unit does it when the mount list changed).
      ExecStartPre = lib.concatStringsSep " " [
        "${netradio}/bin/netradio liq" "--config ${configDir}" "--socket ${liqSocket}"
        "--playlists ${playlistDir}" "--now-dir ${nowDir}" "--port ${toString icecastPort}" "--out ${liqScript}"
      ];
      ExecStart = "${lib.getExe pkgs.liquidsoap} ${liqScript}";
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- the DJ -------------------------------------------------------------------
  # Sequences every station's tracks through Liquidsoap's request queue and,
  # every 3-4 of them, queues a spoken break: what just played, what's next.
  # Kokoro af_heart, same voice + URL order as the switchboard (wallace first,
  # gromit's own container as the fallback). Rendered breaks live under
  # ${djDir}/<mount>/ (last few kept). If it is down, stations shuffle as before.
  systemd.services.netradio-dj = {
    description = "Library radio DJ — sequence tracks, announce every few";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-liquidsoap.service" "docker-open-notebook-kokoro.service" ];
    bindsTo = [ "netradio-liquidsoap.service" ];
    serviceConfig = hardening // {
      User = user;
      Group = user;
      SupplementaryGroups = [ "media" ];   # reads the tracks' tags
      ReadWritePaths = [ djDir nowDir configDir ];   # picks.json / similar.json live in config
      EnvironmentFile = [ "-/run/secrets/lastfm-env" ];  # similar artists for spotlights; optional
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio dj"
        "--now-dir ${nowDir}"
        "--config ${configDir}"
        "--pools ${poolsDir}"
        "--playlists ${playlistDir}"
        "--socket ${liqSocket}"
        "--out ${djDir}"
        "--voice ${kokoroVoice}"
        "--breaks-every 3-4"
        "--profile ${profileJson}"
        "--overrides ${profileOverrides}"
      ] ++ map (u: "--kokoro-url ${u}") kokoroUrls);
      Restart = "always";
      RestartSec = 10;
    };
  };

  # --- wake/idle controller ---------------------------------------------------
  systemd.services.netradio-wake = {
    description = "Start library station encoders on demand, stop them when idle";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-liquidsoap.service" ];
    bindsTo = [ "netradio-liquidsoap.service" ];  # its socket is the whole job
    serviceConfig = hardening // {
      User = user;
      Group = user;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio wake"
        "--stations ${configDir}/stations.json"
        "--playlists ${playlistDir}"
        "--socket ${liqSocket}"
        "--icecast-status http://127.0.0.1:${toString icecastPort}/status-json.xsl"
        "--listen 127.0.0.1 --port ${toString wakePort}"
        "--idle-after 300 --tick 30"
      ];
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- the rain: ambient beds a fixed station loops -----------------------------
  # Freely licensed recordings from the Internet Archive; the fetch is
  # idempotent, so it runs at boot and after a deploy and normally does
  # nothing. Drop your own files in ${ambientDir}/rain and they play too.
  # The state dirs the other units get from netradio-credentials' script are
  # created too late for this one: ReadWritePaths is resolved when the unit
  # starts, and a missing path is 226/NAMESPACE before a line of code runs
  # (2026-09-23, the first deploy of the rain station). tmpfiles runs before
  # any service, so the directory is always there.
  systemd.tmpfiles.rules = [
    "d ${ambientDir} 0755 ${user} ${user} -"
    "d ${ambientDir}/rain 0755 ${user} ${user} -"
    "d ${ambientDir}/rainymood 0755 ${user} ${user} -"
    "d ${ambientDir}/rainymood/src 0755 ${user} ${user} -"
  ];

  systemd.services.netradio-ambient = {
    description = "Fetch the ambient beds (rain) and write the fixed station's playlist";
    wantedBy = [ "multi-user.target" ];
    before = [ "netradio-liquidsoap.service" ];
    after = [ "network-online.target" "netradio-credentials.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = hardening // {
      Type = "oneshot";
      RemainAfterExit = true;
      User = user;
      Group = user;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio ambient"
        "--dir ${ambientDir}"
        "--playlists ${playlistDir}"
      ];
      # ffmpeg/ffprobe: the bed check (a slated sound-effects cut must never
      # reach the station again — 2026-09-23) and the seamless-loop cut
      Environment = [ "PATH=${lib.makeBinPath [ pkgs.ffmpeg ]}" ];
      ReadWritePaths = [ ambientDir playlistDir ];
      TimeoutStartSec = "60min";      # a download plus a 34-minute loop re-encode
    };
  };

  # --- gromit's sound card as a playback endpoint -------------------------------
  # The green jack on the back: one ffplay on a station's mount, ALSA mixer
  # for volume, a small JSON API behind radio.<domain>/speaker/. Starts on
  # the rain so a power cycle brings it back with nothing to press.
  systemd.services.netradio-speaker = {
    description = "Play a library station on gromit's own audio output";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-icecast.service" "netradio-wake.service" "sound.target" ];
    wants = [ "netradio-icecast.service" "netradio-wake.service" ];
    serviceConfig = hardening // {
      User = user;
      Group = user;
      SupplementaryGroups = [ "audio" ];
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio speaker"
        "--icecast http://127.0.0.1:${toString icecastPort}"
        "--wake http://127.0.0.1:${toString wakePort}"
        "--listen 127.0.0.1 --port ${toString speakerPort}"
        "--device plughw:0,0"
        "--card ${speakerCard}"
        "--default-mount ${speakerDefaultMount}"
        "--start-volume ${toString speakerStartVolume}"
        "--start-muted"
        "--ffplay ${pkgs.ffmpeg}/bin/ffplay"
      ];
      Environment = [ "PATH=${lib.makeBinPath [ pkgs.alsa-utils pkgs.ffmpeg ]}" ];
      PrivateDevices = false;         # it needs /dev/snd
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- what the receiver plays on Pandora ---------------------------------------
  # A Pandora station Chris likes is a model for a library station; the log
  # feeds the admin page's "Heard on Pandora" tab and the Lidarr fills.
  systemd.services.netradio-pandora = lib.mkIf config.services.yamaha-ync.enable {
    description = "Log what the receiver plays on Pandora (for growing the library)";
    wantedBy = [ "multi-user.target" ];
    after = [ "yamaha-ync-api.service" ];
    serviceConfig = hardening // {
      User = user;
      Group = user;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio pandora"
        "--api http://127.0.0.1:${toString config.services.yamaha-ync.apiPort}"
        "--out ${configDir}/pandora.jsonl"
        "--interval 15"
      ];
      ReadWritePaths = [ configDir ];
      Restart = "always";
      RestartSec = 30;
    };
  };

  # --- playlist scanner: nightly + shortly after boot --------------------------
  systemd.services.netradio-playlists = {
    description = "Rebuild the library station playlists from genre tags";
    after = [ "netradio-credentials.service" "mnt-fusion.mount" ];
    requires = [ "netradio-credentials.service" ];
    serviceConfig = hardening // {
      Type = "oneshot";
      User = user;
      Group = user;
      SupplementaryGroups = [ "media" ];
      ReadWritePaths = [ playlistDir cacheDir nowDir poolsDir configDir ];
      Nice = 10;
      IOSchedulingClass = "idle";
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio playlists"
        "--config ${configDir}"
        "--out ${playlistDir}"
        "--pools ${poolsDir}"
        "--cache ${tagCache}"
        "--genres ${configDir}/genres.json"   # optional: the beets + Last.fm catalogue's words per file
        "--summary ${nowDir}/stations.json"
        "--ycast ${configDir}/stations.yml"
        "--public-base http://${vtunerHost}/radio"
        "--web-base https://${radioHost}/radio"
        "--quick-picks ${quickPicksJson}"
        "--profile ${profileJson}"
        "--overrides ${profileOverrides}"
      ] ++ map (r: "--root ${r}") libraryRoots);
    };
  };
  systemd.timers.netradio-playlists = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "10min";
      # A deploy that changes this timer re-activates it: rebuild a minute
      # later, so a station added in a PR is not empty until 04:30 (the three
      # era stations shipped empty on 2026-09-16 for exactly that reason).
      OnActiveSec = "1min";
      OnCalendar = "04:30";
      Persistent = true;
      RandomizedDelaySec = "10min";
    };
  };

  # --- the talk profiler: nightly, incremental -------------------------------
  # Listens to every track once (YAMNet + a pitch tracker; see profile.py for
  # the rule and the measurements behind it) and writes profile.json: which
  # tracks are talk (the scanner keeps them off every station) and which
  # start or end in chatter (the DJ doesn't crossfade over those). The first
  # pass over ~17k tracks is a few hours at Nice 19 and idle I/O; after that
  # only new files are analysed. Review what it flagged in profile-report.txt;
  # correct it in profile-overrides.json ({"<path>": "music"} or "talk").
  # Runs before the 04:30 playlist rebuild so exclusions land the same night.
  systemd.services.netradio-profile = {
    description = "Profile library tracks for talk vs music (YAMNet + pitch)";
    after = [ "netradio-credentials.service" "mnt-fusion.mount" ];
    requires = [ "netradio-credentials.service" ];
    # The playlists are what act on the profile: rebuild them the moment a
    # pass finishes rather than at the next 04:30 (the first pass runs past
    # it), so era stations fill the same morning.
    onSuccess = [ "netradio-playlists.service" ];
    # A deploy must not touch a running pass: switch-to-configuration
    # RESTARTS a changed unit and, for a oneshot, WAITS for it — the #305
    # deploy sat in activation for the whole 90-minute re-measure and
    # queued every deploy behind it (2026-09-16 16:55-18:20). The timer
    # (OnActiveSec) still fires the new version once the pass is over.
    restartIfChanged = false;
    stopIfChanged = false;
    serviceConfig = hardening // {
      Type = "oneshot";
      User = user;
      Group = user;
      SupplementaryGroups = [ "media" ];
      ReadWritePaths = [ stateDir ];
      Nice = 19;
      IOSchedulingClass = "idle";
      CPUWeight = 20;
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio profile"
        # library.m3u is every audio file the scanner found, unfiltered. Never
        # a station playlist: those exclude what the profiler flagged, and a
        # profiler fed one drops those tracks' facts as "gone" (11:44 today).
        "--playlist ${playlistDir}/library.m3u"
        "--model ${yamnet}"
        "--profile ${profileJson}"
        "--overrides ${profileOverrides}"
        "--report ${profileReport}"
        "--workers ${toString profileWorkers}"
      ] ++ map (u: "--remote ${u}") profileRemotes);
    };
  };
  systemd.timers.netradio-profile = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "20min";
      # A deploy that changes this timer re-activates it: run shortly after,
      # so a profiler change (new facts, a new server) is exercised the same
      # day rather than waiting for 01:00.
      OnActiveSec = "2min";
      OnCalendar = "01:00";
      Persistent = true;
    };
  };

  # --- the admin API ------------------------------------------------------------
  # Edits the runtime config; files requests under config/requests/ for the
  # privileged side below. Loopback; nginx proxies /admin/api/ to it.
  systemd.services.netradio-admin = {
    description = "netradio admin API (feeds, stations, schedule)";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-credentials.service" ];
    requires = [ "netradio-credentials.service" ];
    serviceConfig = hardening // {
      User = user;
      Group = user;
      ReadWritePaths = [ configDir "${djDir}/inbox" ];
      ExecStart = "${netradio}/bin/netradio admin --config ${configDir} --listen 127.0.0.1 --port ${toString adminPort} --dj-dir ${djDir} --playlists ${playlistDir}";
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- the privileged side: apply and compile -----------------------------------
  # The admin API can't restart units or run the agent, so it drops a request
  # file and this path unit (root) does the work:
  #   apply-*.json    rescan (netradio-playlists), then restart Liquidsoap +
  #                   the wake service ONLY if the station list changed —
  #                   a restart cuts live listeners for a few seconds.
  #   compile-*.json  build a feed's rule from its description: the prompt
  #                   carries the library's artist inventory and the genre
  #                   words; `claude -p` answers with one JSON object (as the
  #                   claude user, the way the weekly digest runs); the
  #                   result is validated and stored, then an apply follows.
  systemd.paths.netradio-apply = {
    wantedBy = [ "multi-user.target" ];
    pathConfig = {
      PathChanged = "${configDir}/requests";
      DirectoryNotEmpty = "${configDir}/requests";
      Unit = "netradio-apply.service";
    };
  };
  systemd.services.netradio-apply = {
    description = "netradio: act on admin requests (rescan / restart / compile a feed)";
    path = [ pkgs.coreutils pkgs.jq pkgs.util-linux pkgs.systemd pkgs.gnugrep ];
    serviceConfig = {
      Type = "oneshot";
      TimeoutStartSec = "30min";
    };
    script = ''
      set -uo pipefail
      req=${configDir}/requests
      shopt -s nullglob
      need_apply=0
      for f in "$req"/compile-*.json; do
        feed="$(jq -r .feed "$f")"
        rm -f "$f"
        [ -n "$feed" ] || continue
        echo "compiling feed $feed"
        work="$(mktemp -d /tmp/netradio-compile.XXXXXX)"
        chown claude "$work"
        if runuser -u claude -- env HOME=/home/claude CLAUDE_AUTONOMOUS=1 \
             PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin \
             bash -c '
               cd /home/claude/nixos-homelab-improvements || exit 1
               ${netradio}/bin/netradio compile-prompt --config ${configDir} --feed "$1" --genre-words ${configDir}/genre-words.json > "$2/prompt" || exit 1
               timeout 10m claude -p "$(cat "$2/prompt")" > "$2/result" 2>/dev/null || exit 1
               ${netradio}/bin/netradio compile-apply --config ${configDir} --feed "$1" --result "$2/result"
             ' _ "$feed" "$work"; then
          echo "feed $feed compiled"
        else
          echo "feed $feed: compile failed (see above)"
          # compile-apply marks the feed failed with the real reason when it
          # ran; only when claude produced nothing at all is there no result
          # file, and then the page should say that instead.
          if [ ! -s "$work/result" ]; then
            printf 'claude -p produced no answer (timeout or failure) — see journalctl -u netradio-apply\n' > "$work/result"
            ${netradio}/bin/netradio compile-apply --config ${configDir} --feed "$feed" --result "$work/result" >/dev/null 2>&1 || true
          fi
        fi
        rm -rf "$work"
        need_apply=1
      done
      for f in "$req"/apply-*.json; do
        echo "apply requested: $(jq -r '.reason // ""' "$f")"
        rm -f "$f"
        need_apply=1
      done
      [ "$need_apply" = 1 ] || exit 0
      systemctl start netradio-playlists.service
      # restart the audio chain only if the mount list changed
      want="$(jq -r '.[].mount' ${configDir}/stations.json | sort)"
      have="$(grep -oP '^station\("\K[^"]+' ${liqScript} 2>/dev/null | sort || true)"
      if [ "$want" != "$have" ]; then
        echo "station list changed: restarting liquidsoap + wake"
        systemctl restart netradio-liquidsoap.service netradio-wake.service
      else
        echo "station list unchanged: no restart"
      fi
    '';
  };

  # --- YCast: the vTuner directory ------------------------------------------
  systemd.services.ycast = {
    description = "YCast — vTuner internet radio directory emulation";
    wantedBy = [ "multi-user.target" ];
    # YCast decides ONCE at startup whether "My Stations" exists (it looks
    # for stations.yml then, never again). The 2026-09-16 13:04 deploy
    # started it before anything had written the file at its new path, and
    # the receiver's menu read "'My Stations' feature not configured." for
    # the rest of the day. The scanner is the only writer, and it runs on a
    # timer — so seed the file with the Quick Picks (JSON is YAML) when it
    # is missing, before YCast looks.
    after = [ "network-online.target" "netradio-credentials.service" ];
    wants = [ "network-online.target" ];
    environment.HOME = "/var/lib/ycast";  # ~/.ycast/cache: resized station icons
    serviceConfig = hardening // {
      DynamicUser = true;
      StateDirectory = "ycast";
      ExecStartPre = "+${pkgs.writeShellScript "ycast-seed-stations" ''
        f=${configDir}/stations.yml
        [ -s "$f" ] || install -o netradio -g users -m 664 ${ycastSeedYaml} "$f"
      ''}";
      ExecStart = "${lib.getExe ycast} -l 127.0.0.1 -p ${toString ycastPort} -c ${configDir}/stations.yml";
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- Last.fm API key (curation plan, phase 2) ------------------------------
  # Genre tags (beets lastgenre) and similar-artist lookups for the DJ's
  # artist tranches. Read-only use. Owner netradio; group users so the agent's
  # catalogue jobs (run as claude) can read it too — a low-value key.
  # Handed over via the secrets-inbox 2026-09-16; edit with `sops secrets/lastfm-env.yaml`.
  sops.secrets."lastfm-env" = {
    sopsFile = ../../../secrets/lastfm-env.yaml;
    key = "lastfm-env";
    owner = user;
    group = "users";
    mode = "0440";
  };

  environment.systemPackages = [ netradio ];
}
