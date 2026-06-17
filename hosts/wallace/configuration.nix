# WALLACE — the compute node (Ryzen 9 5900X / 31 GB / RX 580). Dual-boots with
# Windows. Holds no media pool yet (drives stay on gromit); takes heavy CPU/GPU
# loads offloaded from gromit. See docs: wallace-gromit-split.
{ config, lib, pkgs, ... }:
{
  imports = [
    ./immich-ml.nix   # Immich ML inference offloaded from gromit (CPU on the 5900X)
    ./llm.nix         # local LLM stack (llama.cpp GPU+CPU + Open WebUI)
  ];

  # Dual-boot: GRUB (EFI) with os-prober so the menu lists NixOS + Windows.
  # Windows lives on its own ESP (980 PRO); os-prober finds bootmgfw.efi and
  # adds a "Windows Boot Manager" entry — switch OS from the menu, no BIOS.
  boot.loader.grub = {
    enable = true;
    efiSupport = true;
    device = "nodev";
    useOSProber = true;
    configurationLimit = 10;
  };
  boot.loader.efi.canTouchEfiVariables = true;

  hardware.enableRedistributableFirmware = true;   # amdgpu (RX 580) firmware

  networking.hostName = "wallace";
  networking.networkmanager.enable = true;
  time.timeZone = "America/New_York";

  # Tailscale — stable identity on the tailnet (`wallace`), for management, the
  # gromit↔wallace remote Nix builder, and NFS. One-time auth after first deploy:
  # `sudo tailscale up` (interactive) or with a pre-auth key.
  services.tailscale.enable = true;

  services.openssh = {
    enable = true;
    settings.PermitRootLogin = "no";
    settings.PasswordAuthentication = false;
  };

  users.users.chris = {
    isNormalUser = true;
    extraGroups = [ "wheel" "networkmanager" ];
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII72tYB6OdaFY3kAOYk7A/AEa9hrbckKe6gCoeM1SRhB chris@mcp-server-claude-20260515"
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINVktIfvgHToKIFCsdw1IsuE9e88Yps9oMdY2px622Nf claude-gromit-tmp-dadpc-recon"
    ];
  };
  # Bootstrap convenience — passwordless sudo for wheel; tighten once settled.
  security.sudo.wheelNeedsPassword = false;

  # Remote Nix builder account — gromit's nix-daemon offloads builds here over
  # Tailscale (gromit side: modules/nix-remote-builder.nix). trusted-user so it
  # can import build inputs + realise outputs.
  users.groups.nixremote = {};
  users.users.nixremote = {
    isSystemUser = true;
    group = "nixremote";
    home = "/var/lib/nixremote";
    createHome = true;
    shell = pkgs.bashInteractive;          # nologin breaks nix-store --serve over ssh
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGuN6T9uUZNWmr1nbO/K5eo3kgMKKCzLXazCKU4BUKNY gromit-nix-builder->wallace"
    ];
  };
  nix.settings.trusted-users = [ "root" "nixremote" ];

  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  environment.systemPackages = with pkgs; [ git vim htop tmux efibootmgr ];

  system.stateVersion = "25.11";
}
