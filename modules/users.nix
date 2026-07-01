# User accounts, groups, login behaviour.
{ config, lib, pkgs, ... }:

{
  # Primary user.
  users.users.chris = {
    isNormalUser = true;
    description = "chris";
    extraGroups = [ "networkmanager" "wheel" "media" "lp" ];
    openssh.authorizedKeys.keys = [
      # Chris's personal workstation key (same key declared on wallace).
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFbsMI9lXpM1bi2fR2Ew1DnubEMGcdl3MuFNbqyyn0xI chris@saenzmail.net"
    ];
    packages = with pkgs; [
      firefox
    #  thunderbird
    ];
  };

  # Media group — shared by Jellyfin, Audiobookshelf, etc.
  users.groups.media = { };

  # Root is key-only (PermitRootLogin prohibit-password, set in networking.nix).
  # This automation key replaces chris's passwordless sudo as the way tooling
  # reaches root (security review 2026-06-04): privileged automation logs in as
  # root@ directly, human logins use the chris account where sudo now prompts.
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII72tYB6OdaFY3kAOYk7A/AEa9hrbckKe6gCoeM1SRhB chris@mcp-server-claude-20260515"
  ];

  # Passwordless sudo for wheel — restored 2026-06-07 (the root automation key
  # can't be used from on-box tooling, so chris→root via sudo is the working
  # path). Single-user, Tailscale-only/key-only access; see security review.
  security.sudo.wheelNeedsPassword = false;

  # Automatic login: was OFF (2026-06-04 security review) so the greeter prompts
  # for chris's password. RE-ENABLED 2026-07-01 into the Plasma X11 session, because
  # MeshCentral's remote desktop attaches only to a LOGGED-IN X session (it can't
  # capture the pre-login greeter), and gromit is headless — so a session must
  # always be up for remote GUI access to work at all. Trade-off accepted: no
  # console password, mitigated by headless + Tailscale-gate + MeshCentral admin-lock.
  services.displayManager.autoLogin.enable = true;
  services.displayManager.autoLogin.user = "chris";
}
