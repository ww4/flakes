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
# Library stations are genre-tag playlists (netradio-playlists, nightly) fed
# to Icecast by Liquidsoap. Encoders are ON DEMAND: a station's MP3 encoder
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
  runDir = "/run/netradio";
  liqSocket = "${runDir}/liquidsoap.sock";

  # Where the music is. The first is the Jellyfin library; the second is
  # where Lidarr puts new albums (lidarr.nix).
  libraryRoots = [ "/mnt/fusion/Music" "/mnt/fusion/arr/media/music" ];

  # The station catalogue — ONE definition feeds the scanner (stations.json),
  # Liquidsoap (one output per entry) and the receiver's menu (stations.yml).
  # `genres` are matched as whole words against each track's genre tag,
  # lower-cased with dashes as spaces ("Old-Time" → "old time"); null means
  # every track. Names show on a 2-line receiver display: keep them short.
  stations = [
    { mount = "all";        name = "Everything";           genres = null; }
    { mount = "bluegrass";  name = "Bluegrass & Old-Time"; genres = [ "bluegrass" "old time" "oldtime" "newgrass" "string band" "brother duets" "appalachian" ]; }
    { mount = "country";    name = "Country";              genres = [ "country" "western swing" "honky tonk" "western" ]; }
    { mount = "folk";       name = "Folk";                 genres = [ "folk" "singer songwriter" "celtic" "traditional" "americana" "acoustic" "irish" ]; }
    { mount = "rock";       name = "Rock";                 genres = [ "rock" "alternative" "pop" "punk" "metal" "indie" "new wave" ]; }
    { mount = "blues-jazz"; name = "Blues, Jazz & Soul";   genres = [ "blues" "jazz" "soul" "r&b" "funk" "big band" "swing" "motown" "zydeco" ]; }
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
      s = playlist(id="pl_" ^ mount, mode="randomize", reload_mode="watch",
                   "${playlistDir}/" ^ mount ^ ".m3u")
      s = crossfade(s)
      s = mksafe(s)
      output.icecast(%mp3(bitrate=192), id=mount, start=false,
                     host="127.0.0.1", port=${toString icecastPort}, password=password,
                     mount="/" ^ mount ^ ".mp3", name=name, genre=name,
                     description="Library station", public=false, s)
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
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString ycastPort}";
      recommendedProxySettings = true;
    };

    # Library streams. auth_request wakes the encoder first; then the
    # listener is proxied straight to Icecast, unbuffered, with the ICY
    # metadata headers passing both ways so the display shows the track.
    locations."/radio/" = {
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
    locations."= /_wake" = {
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
      ] ++ map (r: "--root ${r}") libraryRoots);
    };
  };
  systemd.timers.netradio-playlists = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "10min";
      OnCalendar = "04:30";
      Persistent = true;
      RandomizedDelaySec = "10min";
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

  environment.systemPackages = [ netradio ];
}
