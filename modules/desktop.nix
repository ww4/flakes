# Desktop environment: KDE Plasma on X11, printing, sound, Steam.
{ config, lib, pkgs, ... }:

{
  # X11 + KDE Plasma 6, via SDDM. Switched from GNOME/Wayland because
  # MeshCentral's remote desktop is X11/XTEST-only (no Wayland screen capture),
  # and GNOME 50 has no X11 session. SDDM on X11 gives an X11 greeter so
  # MeshCentral can drive the console AND the login screen (true remote GUI
  # login). See modules/services/meshagent + the meshcentral-project notes.
  # Recovery if a login breaks: boot the previous generation from the GRUB menu.
  services.xserver.enable = true;
  services.displayManager.sddm.enable = true;
  services.displayManager.sddm.wayland.enable = false;   # X11 greeter (MeshCentral-capturable)
  services.desktopManager.plasma6.enable = true;
  services.displayManager.defaultSession = "plasmax11";  # X11 session (MeshCentral needs X11, not Wayland)

  # Do NOT hand systemd-coredump events to DrKonqi. On 2026-09-05 this turned
  # ONE tidy crash into 3082 coredumps and 3.5 GB in about an hour.
  #
  # The plasma6 module wires it unconditionally:
  #   systemd.packages = [ kdePackages.drkonqi ];
  #   systemd.services."drkonqi-coredump-processor@".wantedBy =
  #     [ "systemd-coredump@.service" ];
  # so EVERY process crash by EVERY user — including headless service accounts
  # with no display — is handed to a Qt GUI crash reporter.
  #
  # `drkonqi-coredump-launcher` constructs a QGuiApplication before doing
  # anything else. With no DISPLAY that is fatal, and the backtrace is the whole
  # story:
  #   main -> QGuiApplication() -> createPlatformIntegration()
  #        -> init_platform() -> qFatal() -> abort()
  # The launcher therefore DUMPS CORE WHILE HANDLING A CORE DUMP, which enqueues
  # another event, which launches it again. A self-feeding loop whose only exit
  # is filling the disk. The trigger was incidental — herdr's client aborts on
  # teardown instead of exiting cleanly — and any crashing process would do.
  #
  # Cutting the wantedBy is the minimal fix: drkonqi stays installed and a
  # logged-in Plasma session is otherwise untouched. `environment.plasma6.
  # excludePackages` would be WRONG here — it only filters systemPackages, so
  # the systemd wiring above would survive and point at a missing store path.
  #
  # Trade-off, deliberate: no crash dialog for GUI apps in Chris's desktop
  # session either. Crashes are still captured — `coredumpctl` keeps working,
  # which is the part worth having on this box.
  systemd.services."drkonqi-coredump-processor@".wantedBy = lib.mkForce [ ];

  # Force an explicit ":0" as the FIRST X server argument. SDDM launches X via
  # -displayfd (no display token on the command line), but MeshCentral's agent
  # discovers the display by parsing the X process command line for a ":N" token
  # (monitor-info.js getXInfo: `if($4~/^:/) display=$4`). With no token it gets
  # display="" and the remote desktop can't attach. mkBefore puts ":0" at field
  # $4 (right after the X binary). Safe alongside -displayfd on xorg-server ≥21.1
  # (explicit display bypasses displayfd's auto-selection; the number is still
  # written to the fd). The agent then also reads the live -auth cookie from the
  # same command line, so no Xauthority path needs hardcoding.
  services.xserver.displayManager.xserverArgs = lib.mkBefore [ ":0" ];
  # NB: auto-login (needed for MeshCentral's remote desktop on this headless box)
  # is set in modules/users.nix, next to the security-review note it reverses.

  # Keymap.
  services.xserver.xkb = {
    layout = "us";
    variant = "";
    options = "caps:super";
  };

  # Printing — CUPS + drivers, with mDNS discovery via Avahi.
  services.printing.enable = true;
  services.printing.drivers = with pkgs; [
    gutenprint
    brlaser
  ];
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
  };

  # Sound via PipeWire.
  security.rtkit.enable = true;
  services = {
    pulseaudio.enable = false;
    pipewire = {
      enable = true;
      alsa.enable = true;
      alsa.support32Bit = true;
      pulse.enable = true;
    };
  };

  # Steam. Security review 2026-06-04: the openFirewall flags opened the Steam
  # ports on ALL interfaces (LAN + public IPv6). Disabled them and re-opened the
  # same port set scoped to the LAN + Tailscale only.
  programs.steam = {
    enable = true;
    remotePlay.openFirewall = false;       # Steam Remote Play (scoped below)
    dedicatedServer.openFirewall = false;  # Source Dedicated Server (scoped below)
  };
  networking.firewall.interfaces =
    let
      steamTCP = [ 27015 27036 27040 ];
      steamUDP = [ 27015 27036 ];
      steamUDPRanges = [ { from = 27031; to = 27036; } ];  # Remote Play streaming
      steam = {
        allowedTCPPorts = steamTCP;
        allowedUDPPorts = steamUDP;
        allowedUDPPortRanges = steamUDPRanges;
      };
    in
    {
      enp3s0 = steam;      # LAN
      tailscale0 = steam;  # Tailscale
    };
}
