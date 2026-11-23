# Steam on WALLACE, streamed to a client over the tailnet.
#
# WHY STREAMED, AND WHY WALLACE STAYS WHERE IT IS. Chris already streamed
# games from this box to a MacBook, so the path is proven. It is also the only
# option that costs nothing elsewhere: wallace keeps serving gromit's CPU
# handoffs (the 32B LLM, whisper, Kokoro, Immich ML, the netradio profiler)
# from exactly where it sits. Moving the machine or dual-booting Windows would
# take all of that down to play a LEGO game.
#
# ⚠️ A WINDOWS VM WAS RULED OUT BY HARDWARE, not preference. wallace has one
# GPU and no iGPU — the 5900X is not an APU, and `/sys/class/drm` shows only
# `card1`. Passthrough means binding the RX 580 to vfio-pci, which takes it
# from Linux permanently rather than just while playing. Proton gives the same
# games with none of that: every title Chris named is ProtonDB platinum or
# gold (Skywalker Saga, Complete Saga, Hobbit, Myst/Riven classics and the
# 2021 Myst remake platinum; LEGO LOTR and realMyst gold).
#
# ⚠️ RIVEN (2024 REMAKE) WILL NOT RUN HERE and no configuration fixes it. It
# is UE5 and asks RX 5700XT / GTX 1070 8GB with "at least 6GB VRAM" as its
# MINIMUM; the RX 580 has 4 GB and is two generations older. For contrast
# Skywalker Saga's RECOMMENDED is a GTX 780 / R9 290, which this card beats,
# and LEGO Hobbit's minimum is a Radeon X1950 Pro from 2006.
#
# RESOLUTION. A 4K panel is connected on HDMI-A-2 (it advertises 3840x2160 and
# 2560x1440). Render at 1080p: the RX 580 is comfortable there for all of
# these and would struggle with Skywalker Saga at 4K. gamescope upscales.
{ config, lib, pkgs, ... }:

{
  # Steam is unfree. Same line marcus already carries.
  nixpkgs.config.allowUnfree = true;

  programs.steam = {
    enable = true;
    gamescopeSession.enable = true;
  };

  # ⚠️ capSysNice MUST stay false, and the reason is not obvious from here.
  #
  # Setting it true wraps gamescope with `cap_sys_nice+eip`. That `i` is
  # INHERITABLE, so every child gamescope spawns carries the capability —
  # including Steam, which on NixOS runs inside a bubblewrap FHS sandbox.
  # bwrap deliberately refuses to start when it inherits capabilities from a
  # parent that is not setuid, so the session dies the moment Steam launches:
  #
  #   bwrap: Unexpected capabilities but not setuid, old file caps config?
  #   [gamescope] launch: Primary child shut down!
  #
  # Measured on wallace 2026-11-22, not inferred. gamescope itself came up
  # perfectly every time — Vulkan on the RX 580, Xwayland on :0, pipewire
  # connected — and then exited about a second in, on every boot, with greetd
  # falling back to a greeter nobody could log into (chris had no password).
  # The whole thing reads as "the graphical session is broken" when it is
  # really "Steam refused to start".
  #
  # The known workarounds are worse than the problem. Patching out bwrap's
  # guard circumvents an intentional security mechanism and lets anything
  # launched by steam-run inherit the capability too, and it no longer works
  # anyway: Steam restores its own pressure-vessel srt-bwrap on startup.
  #
  # What we give up is gamescope renicing itself. At 1080p on an RX 580, with
  # LEGO-era titles, that is not the binding constraint. A session that starts
  # is worth more than frame pacing under CPU contention.
  programs.gamescope = {
    enable = true;
    capSysNice = false;
  };

  # Game audio has to reach the stream, and wallace has had no sound stack at
  # all until now — it was built as a headless compute node.
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
  };

  # Sunshine: the host half of Moonlight. capSysAdmin is required for Wayland
  # capture (and only for Wayland — it would be wrong to carry it on Xorg).
  services.sunshine = {
    enable = true;
    autoStart = true;
    capSysAdmin = true;
    # NOT openFirewall: that opens every interface including the LAN. Scoped
    # to tailscale0 below instead, matching immich-ml.nix and llm.nix on this
    # host and the house rule that the tailnet is the perimeter.
    openFirewall = false;
  };

  # Sunshine's ports, tailnet only. 47984 HTTPS · 47989 HTTP · 47990 web UI
  # · 48010 RTSP; UDP 47998/47999/48000/48002 video, control, audio, mic.
  networking.firewall.interfaces."tailscale0" = {
    allowedTCPPorts = [ 47984 47989 47990 48010 ];
    allowedUDPPorts = [ 47998 47999 48000 48002 48010 ];
  };

  # The games library: sdb1, a 466 GB Samsung 860 that has been sitting
  # unmounted and unreferenced by this flake.
  #
  # ⚠️ MOUNTED, NOT FORMATTED. A note from 2026-06 says it holds only a stale
  # 2022 Nextcloud sync folder (~54 MB) and is "effectively blank" — but that
  # note is five months old and `claude` has no mount rule here, so it could
  # not be re-verified. Mounting without mkfs means whatever is actually there
  # survives and Steam simply writes alongside it. Reclaiming the space is a
  # separate, deliberate step for Chris.
  fileSystems."/games" = {
    device = "/dev/disk/by-uuid/081945cb-04bd-467d-9397-36a230563cd1";
    fsType = "ext4";
    options = [ "nofail" ];
  };

  # ── the session ────────────────────────────────────────────────────────
  # Without this nothing above actually runs. `services.sunshine` installs a
  # systemd **user** unit, and a user unit needs a logged-in graphical
  # session — wallace has had no display manager at all, so Sunshine would
  # install correctly and never start. Equally, `gamescopeSession` only
  # PROVIDES a session entry (`steam.desktop`); something has to launch it.
  #
  # greetd autologins `chris` straight into `steam-gamescope` on the attached
  # panel. That gives one session serving both uses: Big Picture on the 4K
  # screen if Chris sits at the box, and a compositor for Sunshine to capture
  # when he streams.
  #
  # ⚠️ THIS IS THE PART I COULD NOT TEST. There is a known nixpkgs issue
  # (#419121) that the Steam gamescope session does not invoke
  # `graphical-session.target`, which is exactly what Sunshine's user unit
  # keys off — so Sunshine may need a nudge or a `wantedBy` override on first
  # boot. Failure is contained: sshd and every offload service are SYSTEM
  # units and are unaffected if this session never comes up.
  services.greetd = {
    enable = true;
    settings = {
      initial_session = {
        command = "steam-gamescope";
        user = "chris";
      };
      # greetd requires a default; agreety is built in, so no extra greeter
      # package. It is the fallback if the autologin session exits.
      default_session = {
        command = "${pkgs.greetd.greetd}/bin/agreety --cmd steam-gamescope";
        user = "greeter";
      };
    };
  };

  # So the hardware-encode claim can be CHECKED rather than assumed: Polaris
  # has VCE and `/dev/dri/renderD128` exists, but nothing on this box could
  # report what VAAPI actually offers. `vainfo` answers it in one command.
  environment.systemPackages = with pkgs; [ libva-utils ffmpeg mangohud ];
}
