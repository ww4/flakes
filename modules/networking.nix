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

  # Do NOT accept the tailnet's DNS config. gromit owns its own resolution --
  # Blocky provides split-horizon so *.rosemaryacres.com keeps working with the
  # WAN down (see services/blocky.nix and the offline-DNS design).
  #
  # MagicDNS was enabled tailnet-wide on 2026-08-27 for one reason: Tailscale
  # Funnel needs a Tailscale HTTPS cert, and Tailscale will not issue one unless
  # MagicDNS is on. That was the last blocker on the homelab-mcp Funnel attempt.
  # Funnel was then abandoned -- on 443 it bound the tailnet address and collided
  # with nginx (every vhost down for two hours); on 8443 Anthropic never
  # connected at all. The connector now goes through the lock3 VPS jump host with
  # an ordinary Let's Encrypt cert, so nothing here needs MagicDNS.
  #
  # But accepting it rewrote /etc/resolv.conf to the tailnet resolvers
  # (100.100.100.100 / fd7a:115c:a1e0::53). Docker containers inherit that file
  # and CANNOT reach either address from their own netns, so every container lost
  # outbound DNS while looking healthy -- docker's embedded resolver still
  # answers container-to-container names, so only external lookups failed
  # (jellyseerr: 782 EAI_AGAIN on api.themoviedb.org in a single boot).
  #
  # Per-node on purpose: turning MagicDNS off tailnet-wide in the admin console
  # would also hit marcus, wallace, lock3 and agent-vm, none of which run Blocky.
  services.tailscale.extraSetFlags = [ "--accept-dns=false" ];

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
    # enp6s0u1 — the ASIX AX88179 USB 3.0 gigabit adapter, the standby LAN NIC
    # if the onboard r8169 port fails. Mirrors enp3s0 so moving the cable is a
    # cable move and nothing else. Without this the adapter comes up, takes a
    # DHCP lease from NetworkManager, and looks fine while SSH, Jellyfin-direct
    # and (see blocky.nix) LAN DNS are all silently closed on it -- the failure
    # would present as "the network works but half the house is broken".
    # Harmless while unplugged: an interface with no link accepts nothing.
    # Added 2026-08-25, when enp3s0 fell back to 100Mbps/Full and stayed there
    # across a cable swap, making a NIC-vs-cable swap a live possibility.
    interfaces."enp6s0u1".allowedTCPPorts = [ 22 8096 ];
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
