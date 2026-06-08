# Networking: hostname, NetworkManager, Tailscale, firewall, SSH.
{ config, lib, pkgs, ... }:

{
  networking.hostName = "gromit";

  networking.networkmanager.enable = true;

  # Disable Network Manager Wait (issue on 11/3/23).
  systemd.services.NetworkManager-wait-online.enable = lib.mkForce false;
  systemd.services.systemd-networkd-wait-online.enable = lib.mkForce false;

  # Tailscale overlay network.
  services.tailscale.enable = true;
  networking.firewall.checkReversePath = "loose";

  # Firewall.
  # Security review 2026-06-04: trimmed world-open ports. Web (80/443) stays on
  # all interfaces but is source-gated to Tailscale + LAN at the HTTP layer
  # (services/nginx-access.nix). Removed: 631 (CUPS, not needed off-box), 3000
  # (Homepage) and 9090 (Prometheus) -- both backends bind 127.0.0.1 so those
  # holes were dead anyway. Jellyfin's direct port (8096, 0.0.0.0 for Roku/TVs)
  # is moved to LAN + Tailscale only, below.
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [ 80 443 ];
    allowedUDPPortRanges = [
      # FIXME(security-review 2026-06-04): undocumented broad ranges, origin
      # unknown -- left intact pending investigation. Candidates to narrow/drop.
      { from = 2000; to = 4007; }
      { from = 8000; to = 8300; }
    ];
    # LAN + Tailscale only (no longer world-open):
    #   22   - SSH (key-only; see services.openssh below)
    #   8096 - Jellyfin direct (http://<ip>:8096) for Roku/TV clients
    interfaces."enp3s0".allowedTCPPorts = [ 22 8096 ];
    interfaces."tailscale0".allowedTCPPorts = [ 22 8096 ];
  };

  # Remote access. Security review 2026-06-04: key-only (password + keyboard-
  # interactive auth disabled -- all real logins already use publickey), and the
  # port is scoped to LAN + Tailscale via the firewall above (openFirewall=false)
  # rather than open to the public internet/IPv6.
  services.openssh = {
    enable = true;
    openFirewall = false;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      # Root login allowed for keys only (no password) — used by the automation
      # key in users.nix now that chris's passwordless sudo is gone.
      PermitRootLogin = "prohibit-password";
    };
  };
}
