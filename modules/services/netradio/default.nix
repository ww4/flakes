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
# Ops:
#   sudo cat /var/lib/netradio/credentials.env      Icecast passwords (generated)
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
  icecastPort = 8020; # 8000 is audiobookshelf (icecast SEGVs when the bind fails)

  user = "netradio";
  stateDir = "/var/lib/netradio";
  credsEnv = "${stateDir}/credentials.env";
  playlistDir = "${stateDir}/playlists";
  cacheDir = "${stateDir}/cache";
  tagCache = "${cacheDir}/tags.json";
  djDir = "${stateDir}/dj";
  # Its own subdir, like playlists/ and dj/: ${stateDir} itself is root-owned
  # (it holds credentials.env), so the netradio user cannot create the
  # profile.json.tmp the atomic save needs there (2026-09-16 first-run failure).
  profileDir = "${stateDir}/profile";
  profileJson = "${profileDir}/profile.json";
  profileOverrides = "${profileDir}/profile-overrides.json";   # {path: "talk"|"music"}, hand-edited
  profileReport = "${profileDir}/profile-report.txt";

  # YAMNet (Google's AudioSet classifier, 521 classes) as ONNX — a tf2onnx
  # conversion mirrored on Hugging Face, pinned to a commit. ~16 MB, fetched
  # at build time; the classifier the profiler listens with.
  yamnet = pkgs.fetchurl {
    url = "https://huggingface.co/andrelgomes/yamnet-onnx/resolve/8a03a1572569685c42fdbef54ff36435dbaaf689/yamnet.onnx";
    hash = "sha256-FRAEHc4kounoTsVGgHrECK5JbabR7UG8PMumSWI/jhk=";
  };
  runDir = "/run/netradio";
  liqSocket = "${runDir}/liquidsoap.sock";

  # The DJ's voice: the switchboard's Kokoro choice and URL order (wallace
  # first, gromit's own container last) so the house has one voice.
  kokoroVoice = config.services.switchboard.kokoroVoice;
  kokoroUrls = config.services.switchboard.remoteKokoroUrls ++ [ "http://127.0.0.1:8880" ];

  # Where the music is. The first is the Jellyfin library; the second is
  # where Lidarr puts new albums (lidarr.nix).
  libraryRoots = [ "/mnt/fusion/Music" "/mnt/fusion/arr/media/music" ];

  # The station catalogue — ONE definition feeds the scanner (stations.json),
  # Liquidsoap (one output per entry) and the receiver's menu (stations.yml).
  # `genres` are matched as whole words against each track's genre tag,
  # lower-cased with dashes as spaces ("Old-Time" → "old time"); null means
  # every track. `era` restricts a station to the profiler's audio-quality
  # buckets (shellac = old scratchy records, vintage = the tape era, hifi =
  # modern; rules.py); absent = any. A track not yet profiled plays anywhere.
  # Names show on a 2-line receiver display: keep them short.
  # (Chris, 2026-09-16: tinny 1930s sides and modern masters are both worth
  # having but not back to back — so the genre stations skip shellac and the
  # era stations cut across genre.)
  stations = [
    { mount = "all";        name = "Everything";           genres = null; }
    { mount = "scratchy";   name = "Old Scratchy Records"; genres = null; era = [ "shellac" ]; }
    { mount = "vintage";    name = "Vintage";              genres = null; era = [ "vintage" ]; }
    { mount = "modern";     name = "Modern";               genres = null; era = [ "hifi" ]; }
    { mount = "bluegrass";  name = "Bluegrass & Old-Time"; genres = [ "bluegrass" "old time" "oldtime" "newgrass" "string band" "brother duets" "appalachian" ]; era = [ "vintage" "hifi" ]; }
    { mount = "country";    name = "Country";              genres = [ "country" "western swing" "honky tonk" "western" ]; era = [ "vintage" "hifi" ]; }
    { mount = "folk";       name = "Folk";                 genres = [ "folk" "singer songwriter" "celtic" "traditional" "americana" "acoustic" "irish" ]; era = [ "vintage" "hifi" ]; }
    { mount = "rock";       name = "Rock";                 genres = [ "rock" "alternative" "pop" "punk" "metal" "indie" "new wave" ]; era = [ "vintage" "hifi" ]; }
    { mount = "blues-jazz"; name = "Blues, Jazz & Soul";   genres = [ "blues" "jazz" "soul" "r&b" "funk" "big band" "swing" "motown" "zydeco" ]; era = [ "vintage" "hifi" ]; }
    { mount = "gospel";     name = "Gospel";               genres = [ "gospel" "religious" "christian" "hymns" "sacred" "spiritual" ]; }
    { mount = "classical";  name = "Classical";            genres = [ "classical" "baroque" "orchestral" "opera" "chamber" ]; }
    { mount = "holiday";    name = "Holiday";              genres = [ "holiday" "christmas" "xmas" ]; }
  ];
  stationsJson = pkgs.writeText "netradio-stations.json" (builtins.toJSON stations);

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

  # The phone page. preload="none" matters: a page that pre-fetched nine
  # streams would wake nine encoders on open.
  radioIndex = pkgs.writeTextDir "index.html" ''
    <!doctype html>
    <html lang="en">
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Library radio</title>
    <style>
      :root { color-scheme: light dark; }
      body { font: 17px/1.4 system-ui, sans-serif; margin: 0 auto; max-width: 34rem; padding: 1.5rem 1rem; }
      h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
      p.note { margin: 0 0 1.25rem; opacity: .7; font-size: .9rem; }
      ul { list-style: none; margin: 0; padding: 0; }
      li { padding: .6rem 0; border-top: 1px solid rgba(128,128,128,.3); }
      li b { display: block; margin-bottom: .3rem; }
      audio { width: 100%; }
    </style>
    <h1>Library radio</h1>
    <p class="note">Shuffled from the music library. A station starts a few seconds after you press play and switches itself off five minutes after the last listener leaves.</p>
    <ul>
    ${lib.concatMapStrings (s: ''
      <li><b>${lib.escapeXML s.name}</b><audio controls preload="none" src="/radio/${s.mount}.mp3"></audio></li>
    '') stations}
    </ul>
    </html>
  '';

  # YCast's "My Stations" file. Hand-emitted YAML (not toJSON) so the menu
  # order is the catalogue order, not alphabetical.
  yamlLine = name: url: "  ${builtins.toJSON name}: ${builtins.toJSON url}\n";
  stationsYml = pkgs.writeText "ycast-stations.yml" (
    "My Library:\n"
    + lib.concatMapStrings (s: yamlLine s.name "http://${vtunerHost}/radio/${s.mount}.mp3") stations
    + "\nQuick Picks:\n"
    + lib.concatMapStrings (s: yamlLine s.name s.url) quickPicks
  );

  # Liquidsoap: one randomized, crossfaded playlist → Icecast MP3 output per
  # station, every output created STOPPED. netradio-wake starts/stops them
  # over the server socket (`<mount>.start` / `<mount>.stop`). Playlists are
  # reloaded when the scanner rewrites them (inotify).
  liqScript = pkgs.writeText "netradio.liq" ''
    settings.log.stdout := true
    settings.log.file := false
    settings.log.level := 3
    settings.server.socket := true
    settings.server.socket.path := "${liqSocket}"
    settings.server.socket.permissions := 0o600
    settings.server.timeout := -1.

    password = environment.get("ICECAST_SOURCE_PASSWORD")

    def station(mount, name) =
      # The DJ (netradio-dj) feeds q_<mount> two items ahead — tracks it chose,
      # and every few of them a rendered break. The plain shuffle is the
      # fallback: it plays whenever the queue is empty (the first track after a
      # wake, or the DJ being down), and hands back at the next track boundary.
      pl = playlist(id="pl_" ^ mount, mode="randomize", reload_mode="watch",
                    "${playlistDir}/" ^ mount ^ ".m3u")
      q = request.queue(id="q_" ^ mount)
      s = fallback(id="src_" ^ mount, track_sensitive=true, [q, pl])
      # Breaks carry liq_amplify (speech renders ~8 dB under the music).
      s = amplify(1., override="liq_amplify", s)
      s = crossfade(s)
      s = mksafe(s)
      # The first track's metadata is emitted BEFORE the Icecast connection is
      # up (the log shows "now playing" ahead of "Connecting mount"), so the
      # ICY title update for it can be lost and the receiver shows no song
      # until the next track. Keep the last metadata and re-insert it once
      # the mount is connected; the one log line per track is the record of
      # what each station played.
      s = insert_metadata(s)
      last = ref([])
      s.on_metadata(synchronous=true, fun (m) -> begin
        last := m
        log(label=mount, level=3, "now playing: " ^ m["artist"] ^ " - " ^ m["title"])
      end)
      output.icecast(%mp3(bitrate=192), id=mount, start=false,
                     host="127.0.0.1", port=${toString icecastPort}, password=password,
                     mount="/" ^ mount ^ ".mp3", name=name, genre=name,
                     description="Library station", public=false,
                     on_connect={ if last() != [] then s.insert_metadata(last()) end },
                     s)
    end

    ${lib.concatMapStrings (s: ''
      station(${builtins.toJSON s.mount}, ${builtins.toJSON s.name})
    '') stations}
  '';

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
        <sources>${toString (builtins.length stations + 2)}</sources>
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
    locations = {
      "= /" = {
        root = radioIndex;
        tryFiles = "/index.html =404";
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
      install -d -m 0755 -o ${user} -g ${user} ${djDir}
      install -d -m 0755 -o ${user} -g ${user} ${profileDir}
      # Liquidsoap watches each playlist FILE (inotify): one that appears
      # after it started is never picked up, but an empty one that is later
      # rewritten is (verified 2026-09-15). So every station's file exists
      # before the first start; the scanner fills them.
      for m in ${lib.concatMapStringsSep " " (s: s.mount) stations}; do
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
      ReadWritePaths = [ djDir ];
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio dj"
        "--stations ${stationsJson}"
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
        "--stations ${stationsJson}"
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
      ReadWritePaths = [ playlistDir cacheDir ];
      Nice = 10;
      IOSchedulingClass = "idle";
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio playlists"
        "--stations ${stationsJson}"
        "--out ${playlistDir}"
        "--cache ${tagCache}"
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
    serviceConfig = hardening // {
      Type = "oneshot";
      User = user;
      Group = user;
      SupplementaryGroups = [ "media" ];
      ReadWritePaths = [ stateDir ];
      Nice = 19;
      IOSchedulingClass = "idle";
      CPUWeight = 20;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio profile"
        "--playlist ${playlistDir}/all.m3u"
        "--model ${yamnet}"
        "--profile ${profileJson}"
        "--overrides ${profileOverrides}"
        "--report ${profileReport}"
      ];
    };
  };
  systemd.timers.netradio-profile = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "20min";
      OnCalendar = "01:00";
      Persistent = true;
    };
  };

  # --- YCast: the vTuner directory ------------------------------------------
  systemd.services.ycast = {
    description = "YCast — vTuner internet radio directory emulation";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment.HOME = "/var/lib/ycast";  # ~/.ycast/cache: resized station icons
    serviceConfig = hardening // {
      DynamicUser = true;
      StateDirectory = "ycast";
      ExecStart = "${lib.getExe ycast} -l 127.0.0.1 -p ${toString ycastPort} -c ${stationsYml}";
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
