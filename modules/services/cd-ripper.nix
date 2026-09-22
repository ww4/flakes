# CD ripper — insert a disc in any USB drive on the box, walk away.
#
# Chris, 2026-09-19: the local library has the classical and jazz the public
# trackers don't, and he has stacks of CDs; ripping is faster than chasing
# torrents for the obscure stuff. N drives on a powered hub rip in parallel
# (the drive is the bottleneck, nothing else is).
#
#   udev: an audio CD appears in /dev/srN  →  cd-rip@srN.service
#     whipper  — secure rip (cdparanoia, re-reads), AccurateRip verification,
#                MusicBrainz tags, FLAC + cue + log; --unknown so an
#                unlisted disc still rips (tagged Unknown, fixed later)
#     beets    — the same MusicBrainz + Last.fm genre + cover-art chain the
#                library retag used; moves the album into the library as
#                Artist/Album/NN Title.flac (the layout the library has)
#     eject; ntfy says what landed and whether AccurateRip vouched for it;
#     radio.<domain>/rips/ shows what every drive is doing.
#
# One-time per drive model: the read offset, which whipper needs to verify
# against AccurateRip.  With a disc in the drive:  sudo cd-ripper-offset /dev/sr0
# It is written to the ripper's own whipper.conf (state dir), keyed by drive
# model, so the same model on another port needs nothing.
#
#   journalctl -u 'cd-rip@*'            what happened, per drive
#   /var/lib/cd-ripper/logs/            whipper's rip logs (AccurateRip results)
#   /var/lib/cd-ripper/import.log       beets' import decisions
{ config, lib, pkgs, ... }:

let
  cfg = config.services.cd-ripper;
  user = "cd-ripper";
  stateDir = "/var/lib/cd-ripper";
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  beetsConfig = pkgs.writeText "cd-ripper-beets.yaml" ''
    directory: ${cfg.library}
    library: ${stateDir}/beets.db
    plugins: lastgenre fetchart
    threaded: yes
    per_disc_numbering: no
    original_date: no
    import:
      move: yes
      write: yes
      copy: no
      quiet: yes
      quiet_fallback: asis   # whipper already matched the disc; an unsure beets match is not worth a prompt
      log: ${stateDir}/import.log
      duplicate_action: skip
      timid: no
    paths:
      default: $albumartist/$album%aunique{}/$track $title
      comp: Compilations/$album%aunique{}/$track $title
      singleton: $artist/Singles/$title
    lastgenre:
      auto: yes
      count: 5
      separator: '; '
      keep_existing: yes
      source: album
      fallback: ""
    fetchart:
      auto: yes
      sources: filesystem coverart itunes amazon albumart
      cover_names: cover folder front
  '';

  # ---- the rip: one drive, one disc, start to finish ------------------------
  ripScript = pkgs.writeShellApplication {
    name = "cd-rip";
    runtimeInputs = [ pkgs.whipper pkgs.beets pkgs.util-linux pkgs.coreutils pkgs.findutils pkgs.jq gromit-notify ];
    text = ''
      drive=''${1:?usage: cd-rip srN}
      dev=/dev/$drive
      export XDG_CONFIG_HOME=${stateDir}/config XDG_CACHE_HOME=${stateDir}/cache XDG_DATA_HOME=${stateDir}/data HOME=${stateDir}
      work=${stateDir}/work/$drive
      status=${stateDir}/www/status/$drive.json
      mkdir -p "$work" "${stateDir}/logs" "$(dirname "$status")"
      umask 002

      say() {   # state, detail → status file (the page) + journal
        jq -n --arg drive "$drive" --arg state "$1" --arg detail "$2" --arg at "$(date -Is)" \
           '{drive: $drive, state: $state, detail: $detail, at: $at}' > "$status.tmp" && mv "$status.tmp" "$status"
        echo "$drive: $1 — $2"
      }

      # a rip left behind by a crash: out of the way, not over the top of
      rm -rf "''${work:?}"/*
      say ripping "reading the disc"
      set +e
      whipper --eject never cd -d "$dev" rip --unknown --cdr --keep-going --cover-art file \
              --output-directory "$work" --track-template '%A - %d/%t %n' --disc-template '%A - %d/%A - %d' \
              > "$work/whipper.out" 2>&1
      rc=$?
      set -e
      album_dir=$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -1 || true)
      log=$(find "$work" -name '*.log' | head -1 || true)
      verdict="not verified"
      if [ -n "$log" ]; then
        cp "$log" "${stateDir}/logs/$(date +%Y%m%d-%H%M%S)-$(basename "$log")"
        if grep -q 'All tracks accurately ripped' "$log"; then verdict="AccurateRip: all tracks verified"
        elif grep -q 'Some tracks could not be verified' "$log"; then verdict="AccurateRip: $(grep -o '[0-9]*/[0-9]* got no match' "$log" | head -1) unverified"
        elif grep -q 'None of the tracks are present' "$log"; then verdict="not in AccurateRip (unknown pressing)"
        fi
      fi
      tracks=$(find "$work" -name '*.flac' | wc -l)
      if [ "$rc" -ne 0 ] || [ "$tracks" -eq 0 ]; then
        say failed "whipper exit $rc, $tracks tracks — see journalctl -u cd-rip@$drive"
        tail -20 "$work/whipper.out" || true
        eject "$dev" || true
        gromit-notify "CD rip failed ($drive)" "whipper exit $rc, $tracks tracks. journalctl -u cd-rip@$drive" high "cd,x" || true
        exit 1
      fi
      title=$(basename "$album_dir")
      say importing "$title ($tracks tracks, $verdict)"

      # into the library through beets: MusicBrainz tags, Last.fm genres,
      # cover art, and the library's Artist/Album/NN Title layout
      if beet -c ${beetsConfig} import -q "$album_dir" >> "${stateDir}/beets.out" 2>&1; then
        note="→ library"
      else
        note="beets import FAILED; left in $work"
      fi
      # whatever beets did not move (the cue, the log, a cover) is not needed;
      # the library's files were made 0664 under the ripper's umask and the
      # library's setgid dirs give them the media group
      if [ -d "$album_dir" ] && [ -z "$(find "$album_dir" -name '*.flac' | head -1)" ]; then
        rm -rf "$album_dir"
      fi
      eject "$dev" || true
      say "done" "$title ($tracks tracks, $verdict) $note"
      gromit-notify "Ripped: $title" "$tracks tracks, $verdict. $note" low "cd,musical_note" "https://${cfg.statusVirtualHost}/rips/" || true
    '';
  };

  offsetScript = pkgs.writeShellApplication {
    name = "cd-ripper-offset";
    runtimeInputs = [ pkgs.util-linux ];
    text = ''
      # Find (and remember) a drive's read offset: sudo cd-ripper-offset /dev/sr0
      # Needs a disc in the drive that AccurateRip knows — any popular CD.
      dev=''${1:?usage: cd-ripper-offset /dev/srN}
      exec runuser -u ${user} -- env XDG_CONFIG_HOME=${stateDir}/config XDG_CACHE_HOME=${stateDir}/cache HOME=${stateDir} \
           ${pkgs.whipper}/bin/whipper offset find -d "$dev"
    '';
  };

  statusPage = pkgs.writeText "cd-ripper-index.html" ''
    <!doctype html><meta charset="utf-8"><title>CD rips</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>body{font:16px system-ui;margin:2rem auto;max-width:40rem;padding:0 1rem;background:#111;color:#eee}
    .d{border:1px solid #333;border-radius:10px;padding:1rem;margin:.6rem 0}.d b{font-size:1.2rem}
    .ripping{border-color:#e8a33d}.importing{border-color:#5aa9e6}.done{border-color:#4caf50}.failed{border-color:#e0524f}
    small{color:#999}</style>
    <h1>CD rips</h1><p><small>Insert a disc in any drive; it rips, files itself in the library, and ejects. This page refreshes itself.</small></p>
    <div id="drives"><small>loading…</small></div>
    <script>
    async function load(){
      const r = await fetch("status/?t=" + Date.now(), {headers:{Accept:"application/json"}}).catch(()=>null);
      if(!r||!r.ok){document.getElementById("drives").innerHTML="<small>no drives have ripped anything yet</small>";return;}
      const files=(await r.json()).filter(f=>f.name.endsWith(".json"));
      const st=await Promise.all(files.map(f=>fetch("status/"+f.name+"?t="+Date.now()).then(x=>x.json()).catch(()=>null)));
      document.getElementById("drives").innerHTML=st.filter(Boolean).sort((a,b)=>a.drive.localeCompare(b.drive)).map(s=>
        `<div class="d ''${s.state}"><b>''${s.drive}</b> · ''${s.state}<br>''${s.detail}<br><small>''${new Date(s.at).toLocaleString()}</small></div>`).join("")
        || "<small>no drives have ripped anything yet</small>";
    }
    load(); setInterval(load, 5000);
    </script>
  '';
in
{
  options.services.cd-ripper = {
    enable = lib.mkEnableOption "automatic CD ripping on disc insert (whipper + beets)";
    library = lib.mkOption { type = lib.types.str; default = "/mnt/fusion/Music"; description = "where finished albums go (Artist/Album/NN Title.flac)"; };
    group = lib.mkOption { type = lib.types.str; default = "media"; description = "group that owns the library; the ripper joins it"; };
    statusVirtualHost = lib.mkOption { type = lib.types.str; default = "radio.rosemaryacres.com"; description = "nginx vhost that serves /rips/"; };
  };

  config = lib.mkIf cfg.enable {
    users.users.${user} = { isSystemUser = true; group = user; extraGroups = [ "cdrom" cfg.group ]; home = stateDir; };
    users.groups.${user} = { };
    environment.systemPackages = [ offsetScript pkgs.whipper pkgs.beets ];

    systemd.tmpfiles.rules = [
      "d ${stateDir} 0755 ${user} ${user} -"
      "d ${stateDir}/config 0755 ${user} ${user} -"
      "d ${stateDir}/cache 0755 ${user} ${user} -"
      "d ${stateDir}/data 0755 ${user} ${user} -"
      "d ${stateDir}/work 0755 ${user} ${user} -"
      "d ${stateDir}/logs 0755 ${user} ${user} -"
      "d ${stateDir}/www 0755 ${user} ${user} -"
      "d ${stateDir}/www/status 0755 ${user} ${user} -"
      "L+ ${stateDir}/www/index.html - - - - ${statusPage}"
    ];

    # An audio CD landing in any drive starts that drive's rip. `change` is
    # the media event; the audio track count is only set for CD-DA, so data
    # discs and empty trays do nothing. --no-block: udev kills slow RUN
    # programs, and a running rip on the same drive (still importing while
    # the tray is out) makes the start a no-op — the script ejects last, so
    # a new disc cannot be in before it is done.
    services.udev.extraRules = ''
      ACTION=="change", SUBSYSTEM=="block", KERNEL=="sr[0-9]*", ENV{ID_CDROM_MEDIA_TRACK_COUNT_AUDIO}=="?*", \
        RUN+="${pkgs.systemd}/bin/systemctl start --no-block cd-rip@%k.service"
    '';

    systemd.services."cd-rip@" = {
      description = "Rip the audio CD in /dev/%i into the library";
      after = [ "network-online.target" "mnt-fusion.mount" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = user;
        Group = user;
        SupplementaryGroups = [ "cdrom" cfg.group ];
        UMask = "0002";
        ExecStart = "${ripScript}/bin/cd-rip %i";
        WorkingDirectory = stateDir;
        TimeoutStartSec = "2h";                   # a badly scratched disc re-reads for a long time
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ stateDir cfg.library ];
      };
    };

    # radio.<domain>/rips/ — what every drive is doing
    services.nginx.virtualHosts.${cfg.statusVirtualHost}.locations."/rips/" = {
      alias = "${stateDir}/www/";
      extraConfig = ''
        autoindex on;
        autoindex_format json;
        add_header Cache-Control "no-store";
      '';
    };
  };
}
