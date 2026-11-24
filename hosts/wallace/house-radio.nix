# THE HOUSE RADIO ON WALLACE'S OWN SPEAKERS.
#
# The room's powered speakers moved off gromit's green jack and onto wallace's
# (2026-11-23), so the box Chris games on now drives the only speakers in the
# room. That splits the two audio paths by what each one can tolerate: game
# audio goes to the hardware directly, and the radio — which does not care
# about delay — comes over the network from gromit, where netradio already
# publishes it.
#
# ⚠️ THE OBVIOUS DIRECTION IS THE WRONG ONE. Streaming wallace's game audio TO
# gromit was the first idea, and it cannot work: Icecast exists in order to
# buffer, so a game's sound would arrive seconds behind the picture. Inverting
# it puts the latency-free path on hardware and the latency-tolerant one on the
# wire, and needs no new streaming infrastructure at all — wallace just becomes
# another listener on the same mount the Yamaha already pulls.
#
# ⚠️ WHY A *USER* SERVICE, NOT A SYSTEM ONE — netradio's own speaker component
# is the wrong tool here, and its own help string says why:
#
#     --device   ALSA device to open; NOT `default`, which PipeWire claims
#
# It opens the hardware PCM directly and sets volume through amixer. On gromit
# that is correct (a system user with no session, driving the card itself). On
# wallace it is wrong twice over: PipeWire has to own the card so Steam can
# play, so a second process grabbing the PCM means one of them gets silence;
# and amixer is the *hardware* level, so radio volume would drag game volume
# with it. An ordinary PipeWire client has neither problem — PipeWire mixes the
# two streams and each keeps its own level. Hence ffmpeg -f pulse, in chris's
# session.
#
# MEASURED 2026-11-23, not assumed: the mount answers 200 audio/mpeg over the
# LAN, ffmpeg decodes it at 192 kb/s 44.1 kHz stereo, the `pulse` muxer is
# compiled into this ffmpeg, and Chris confirmed by ear that the radio plays
# over the game.
{ config, lib, pkgs, ... }:

let
  # gromit's LAN address, and the vhost that fronts netradio's Icecast.
  #
  # Repeated rather than read: gromit and wallace are separate nixosSystems, so
  # there is no shared config to read it from. This matches what netradio
  # already does for the same value (it repeats the LAN IP with a pointer to
  # blocky.nix's proxyIP, because reading it while also defining it is an
  # infinite recursion).
  gromitLan = "192.168.1.65";
  radioHost = "radio.rosemaryacres.com";

  # Mount names known at the time of writing, for the usage text only. The
  # authoritative list is gromit's /var/lib/netradio/config/stations.json plus
  # the relays; wallace deliberately keeps no copy of it, so this cannot drift
  # into being a second source of truth — an unknown mount simply 404s.
  knownMounts = "all, scratchy, bluegrass, rainymood, ir-hank-fm-105-5, ir-froggy-104-9";

  house-radio = pkgs.writeShellScriptBin "house-radio" ''
    set -euo pipefail

    envf="''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/house-radio.env"

    usage() {
      cat <<'EOF'
    house-radio — play a netradio station on this machine's speakers

      house-radio on <mount> [percent]   start a station (percent 1-100, default 40)
      house-radio off                    stop whatever is playing
      house-radio status                 show what is playing, if anything

    It mixes with game audio instead of replacing it, and its volume is
    independent of the game's. <mount> is a netradio mount name; the
    authoritative list lives on gromit in
    /var/lib/netradio/config/stations.json. Known when this was written:
    EOF
      echo "  ${knownMounts}"
    }

    stop_all() {
      # The glob matches whichever instance is up; `stop` on a unit that is
      # already down is not an error, so this is safe to run twice.
      ${pkgs.systemd}/bin/systemctl --user stop 'house-radio@*.service' 2>/dev/null || true
    }

    case "''${1:-}" in
      on)
        mount="''${2:-}"
        [ -n "$mount" ] || { echo "house-radio: 'on' needs a mount name" >&2; usage >&2; exit 2; }
        pct="''${3:-40}"
        case "$pct" in
          ""|*[!0-9]*) echo "house-radio: percent must be a whole number 1-100" >&2; exit 2 ;;
        esac
        [ "$pct" -ge 1 ] && [ "$pct" -le 100 ] || { echo "house-radio: percent must be 1-100, got '$pct'" >&2; exit 2; }

        # ffmpeg's volume filter takes a multiplier, not a percentage.
        gain=$(${pkgs.coreutils}/bin/printf '0.%02d' "$pct")
        [ "$pct" -eq 100 ] && gain=1.0

        # Written before the start so the unit's EnvironmentFile picks it up.
        # install(1) rather than a redirect: it creates the file with the mode
        # we want in one step instead of a world-readable file we then chmod.
        ${pkgs.coreutils}/bin/printf 'RADIO_VOLUME=%s\n' "$gain" \
          | ${pkgs.coreutils}/bin/install -m 0600 /dev/stdin "$envf"

        stop_all
        ${pkgs.systemd}/bin/systemctl --user start "house-radio@$mount.service"
        echo "house-radio: $mount at $pct%"
        ;;
      off)
        stop_all
        echo "house-radio: stopped"
        ;;
      status)
        if u=$(${pkgs.systemd}/bin/systemctl --user list-units --no-legend --state=running 'house-radio@*.service' 2>/dev/null) \
           && [ -n "$u" ]; then
          echo "$u"
          [ -r "$envf" ] && ${pkgs.coreutils}/bin/cat "$envf"
        else
          echo "house-radio: nothing playing"
        fi
        ;;
      ""|-h|--help|help) usage ;;
      *) echo "house-radio: unknown command '$1'" >&2; usage >&2; exit 2 ;;
    esac
  '';
in
{
  # PipeWire asks rtkit for realtime priority and there is no rtkit-daemon on
  # this host to ask — `systemctl is-active rtkit-daemon` reads `inactive`, and
  # pipewire logs `RTKit error: org.freedesktop.DBus.Error.ServiceUnknown` three
  # times per start, falling back to MaxRealtimePriority=1. Without RT priority
  # an audio thread competing with a game for the 5900X is how you get crackle
  # and dropouts under load. This is the one line that fixes it.
  security.rtkit.enable = true;

  # ⚠️ RESOLVE THE RADIO VHOST TO GROMIT'S *LAN* ADDRESS, NOT ITS TAILNET ONE.
  #
  # wallace resolves radio.rosemaryacres.com to 100.82.117.116 (gromit on the
  # tailnet) and cannot reach it: tailscaled on wallace logs
  #
  #   open-conn-track: flow TCP 100.66.171.120:* > 100.82.117.116:443
  #                    rejected due to acl
  #
  # 10,795 times in a single boot. That same rejection is why comin on this
  # host has been unable to pull from Forgejo since 2026-11-08 and has been
  # riding the GitHub mirror — it is an ACL rejection, not the i/o timeout it
  # looks like from comin's side. The tailnet policy deserves its own fix; this
  # entry is not that fix, it just stops the radio depending on it.
  #
  # The LAN path is the better one here regardless: both machines are in the
  # same room on the same /24, so it avoids a DERP hop for a continuous stream.
  # Verified 2026-11-23 — with the name resolved to the LAN address, the mount
  # returns 200 audio/mpeg and the TLS certificate still matches, because nginx
  # serves the same vhost on both addresses.
  networking.hosts."${gromitLan}" = [ radioHost ];

  environment.systemPackages = [ house-radio ];

  # One template unit; the instance name is the netradio mount. Started on
  # demand by `house-radio on <mount>` and deliberately NOT wantedBy anything —
  # logging in should not start playing music at somebody.
  systemd.user.services."house-radio@" = {
    description = "House radio (netradio mount %i) on this machine's speakers";

    # This unit only makes sense in the session that owns the audio device.
    # NixOS defines user units for every user, and the agent account has no
    # session and no business starting it.
    unitConfig.ConditionUser = "chris";

    serviceConfig = {
      Type = "exec";

      # Environment first, EnvironmentFile second, because the file is read
      # after and so overrides it. That ordering is what gives a default
      # without shell expansion — systemd substitutes ${VAR} in ExecStart but
      # does NOT implement ${VAR:-default}, so a bare default would have
      # needed a shell wrapper.
      Environment = [ "RADIO_VOLUME=0.4" ];
      EnvironmentFile = "-%t/house-radio.env";

      # -reconnect*: a continuous HTTP stream will drop eventually; without
      # these ffmpeg exits and the music stops for good.
      # The mount is woken by nginx's auth_request hook on gromit, so simply
      # requesting it starts the Liquidsoap output if it was not running.
      ExecStart = lib.concatStringsSep " " [
        "${pkgs.ffmpeg}/bin/ffmpeg -nostdin -hide_banner -loglevel warning"
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        "-i https://${radioHost}/radio/%i.mp3"
        "-filter:a volume=\${RADIO_VOLUME}"
        "-f pulse"
        "\"House radio (%i)\""
      ];

      Restart = "on-failure";
      RestartSec = "5s";
    };
  };
}
