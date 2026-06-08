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
    # (Removed the undocumented UDP ranges 2000-4007 and 8000-8300 on 2026-06-08:
    # investigation found NOTHING listening on UDP in either range — every UDP
    # listener on the box (68/546/5353/7359/9094/41641/ephemeral) is outside them,
    # and all web services are loopback-bound behind nginx. They were dead inbound
    # holes. Re-add a scoped range here if a future service needs inbound UDP.)
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
