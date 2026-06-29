# MARCUS — Lenovo ThinkPad T480 laptop. Dual-user daily driver (chris + mary),
# intermittently online. KDE Plasma 6 + Hyprland on Wayland, fingerprint unlock
# (T480 06cb:009a sensor via the pinned flake input), CUPS/avahi printing,
# PipeWire, NetworkManager + iwd.
#
# Joined the fleet flake 2026-06-24 — was its own ~/nixos-config flake on
# nixpkgs-25.05; now rides nixos-unstable alongside gromit/wallace and applies
# `main` via comin GitOps (catches up whenever the laptop is online). Laptop/
# desktop concerns live here; shared infra is imported from ../../modules.
{ config, lib, pkgs, ... }:
{
  imports = [
    ./hardware-configuration.nix
    ./network-tools.nix                       # iwd WiFi backend + network troubleshooting tools

    # Shared fleet infra. (Home-Manager for chris is configured inline below so
    # marcus can layer ./hyprland.nix on top of the shared ../../home config.)

    # Scoped Claude agent — same model as gromit (own uid, no wheel, read-only
    # standing access; privileged actions go through comin or the sudo allowlist).
    ../../modules/agent/claude-user.nix
    ../../modules/agent/sudo.nix              # scoped sudoers (gromit-centric rules are inert here)
    ../../modules/agent/claude-harness.nix    # root-owned managed settings + PreToolUse guard
    ../../modules/agent/comin.nix             # GitOps applier — builds nixosConfigurations.marcus
  ];

  # --- Boot (laptop: systemd-boot, unlike gromit/wallace's GRUB) ---
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.timeout = 1;                    # don't sit ~5s at the menu every boot

  # --- Networking ---
  networking.hostName = "marcus";
  networking.networkmanager.enable = true;    # iwd backend is set in ./network-tools.nix
  # No cellular/WWAN modem on this T480 — skip the ModemManager daemon NM pulls in.
  systemd.services.ModemManager.enable = false;
  services.tailscale.enable = true;
  networking.firewall.checkReversePath = "loose";   # required for Tailscale exit/subnet routing

  time.timeZone = "America/New_York";
  i18n.defaultLocale = "en_US.UTF-8";
  i18n.extraLocaleSettings = {
    LC_ADDRESS = "en_US.UTF-8";
    LC_IDENTIFICATION = "en_US.UTF-8";
    LC_MEASUREMENT = "en_US.UTF-8";
    LC_MONETARY = "en_US.UTF-8";
    LC_NAME = "en_US.UTF-8";
    LC_NUMERIC = "en_US.UTF-8";
    LC_PAPER = "en_US.UTF-8";
    LC_TELEPHONE = "en_US.UTF-8";
    LC_TIME = "en_US.UTF-8";
  };

  # --- Desktop: KDE Plasma 6 + Hyprland (Wayland) ---
  services.displayManager.sddm = {
    enable = true;
    wayland.enable = true;
  };
  services.desktopManager.plasma6.enable = true;
  programs.hyprland = {
    enable = true;
    xwayland.enable = true;
  };
  environment.sessionVariables.NIXOS_OZONE_WL = "1";   # hint Electron/Chromium to use Wayland
  hardware.graphics.enable = true;
  services.xserver.xkb = {
    layout = "us";
    variant = "";
  };

  # --- Printing (CUPS) + mDNS ---
  services.printing.enable = true;
  services.printing.drivers = with pkgs; [ gutenprint brlaser ];
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
  };

  # --- Audio (PipeWire) ---
  services.pulseaudio.enable = false;
  security.rtkit.enable = true;
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    alsa.support32Bit = true;
    pulse.enable = true;
  };

  # --- Users (chris + mary). chris also gets Home-Manager (above) and is the
  # account the scoped agent operates alongside. Passwords stay mutable
  # (set with passwd) — don't lock out a shared physical laptop. ---
  #
  # The shared agent module (claude-user.nix) puts the agent in the `media`
  # group, which gromit defines in modules/users.nix (not imported here).
  # Declare an empty one so activation doesn't fail; it carries nothing on a
  # laptop with no media pool.
  users.groups.media = { };

  users.users.chris = {
    isNormalUser = true;
    description = "Chris";
    extraGroups = [ "networkmanager" "wheel" "lp" ];
    openssh.authorizedKeys.keys = [
      # Chris's personal workstation key (same key declared on gromit + wallace).
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFbsMI9lXpM1bi2fR2Ew1DnubEMGcdl3MuFNbqyyn0xI chris@saenzmail.net"
    ];
    packages = with pkgs; [ kdePackages.kate ];
  };
  users.users.mary = {
    isNormalUser = true;
    description = "Mary";
    extraGroups = [ "networkmanager" "wheel" "lp" ];
    packages = with pkgs; [ kdePackages.kate ];
  };
  security.sudo.wheelNeedsPassword = false;   # marcus's existing behavior (physical, single-household)

  # Home-Manager for chris: the shared fleet home (../../home: shell/git/vscode/
  # packages) PLUS marcus's Hyprland desktop (./hyprland.nix). Mirrors the knobs
  # from modules/home-manager.nix. mary keeps plain KDE (no HM).
  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    backupFileExtension = "hm-backup";
    users.chris = {
      imports = [ ../../home ./hyprland.nix ];
    };
  };

  programs.firefox.enable = true;
  programs.appimage = {
    enable = true;
    binfmt = true;
  };
  nixpkgs.config.allowUnfree = true;
  services.flatpak.enable = true;

  # --- Fingerprint sensor (ThinkPad T480, 06cb:009a) ---
  # Module comes from the nixos-06cb-009a-fingerprint-sensor flake input (wired
  # into nixosConfigurations.marcus in flake.nix).
  services."06cb-009a-fingerprint-sensor" = {
    enable = true;
    backend = "python-validity";
  };
  security.pam.services.sudo.fprintAuth = true;
  security.pam.services.polkit-1.fprintAuth = true;
  security.pam.services.login.fprintAuth = true;
  security.pam.services.sddm.fprintAuth = true;
  # hyprlock authenticates the PASSWORD through this PAM service. Fingerprint on
  # the lock screen is handled by hyprlock's OWN fprintd backend (auth.fingerprint
  # in hyprland.nix), NOT PAM — so we deliberately do NOT set fprintAuth here, or
  # pam_fprintd and hyprlock's verify loop would fight over the sensor.
  security.pam.services.hyprlock = { };

  # The reverse-engineered python-validity driver for the 06cb:009a sensor gets
  # wedged after a claim+release (the greeter's fprint login, or a suspend), so
  # hyprlock's in-session verify opens a session but the reader never powers on
  # (no LED). Re-arm the driver on resume so wake-from-sleep → fingerprint works.
  # (Session start is re-armed via exec-once in hyprland.nix for the post-boot case.)
  powerManagement.resumeCommands = ''
    ${pkgs.systemd}/bin/systemctl restart python3-validity open-fprintd
  '';

  # --- SSH (Tailscale-reachable; laptop keeps password auth for console parity) ---
  services.openssh.enable = true;

  # --- System packages (shared by both users; chris layers his own via HM) ---
  environment.systemPackages = with pkgs; [
    # GUI applications
    google-chrome
    element-desktop
    veracrypt
    libreoffice-fresh
    vscode-fhs
    vscode-extensions.rooveterinaryinc.roo-cline
    teams-for-linux
    winbox
    transmission_4-qt

    # Hyprland / Wayland session
    polkit_gnome
    xdg-desktop-portal-hyprland
    xdg-desktop-portal-gtk
    wl-clipboard
    cliphist
    wl-clip-persist
    hyprlock
    hypridle
    wlr-randr
    kanshi
    udiskie
    gvfs
    xdg-utils
    trash-cli
    brightnessctl
    playerctl
    grim
    slurp
    wf-recorder
    swappy
    wofi
    mako
    eww
    nwg-panel
    nwg-drawer
    nwg-look
    lxappearance
    papirus-icon-theme
    nordic
    bibata-cursors
    baobab
    gnome-system-monitor
    gnome-calculator
    gnome-calendar
    evince
    flameshot
    kdePackages.kdeconnect-kde
    blueman

    # Terminal utilities
    easyeffects
    byobu
    wget
    tmux
    htop
    lf
    ncdu
    gparted
    fastfetch
    wireshark
    networkmanager
  ];

  # --- Nix daemon: GC + store optimisation (preserved from marcus's own config) ---
  nix = {
    package = pkgs.nixVersions.stable;
    extraOptions = "experimental-features = nix-command flakes";
    optimise = {
      automatic = true;
      dates = [ "03:45" ];
    };
    gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 30d";
    };
  };

  # NixOS release marcus was first installed from — leave pinned.
  system.stateVersion = "25.05";
}
