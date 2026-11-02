# Blocky — local forwarding DNS with split-horizon for rosemaryacres.com.
#
# Purpose (offline-hardening, 2026-07-06): keep local services reachable BY
# HOSTNAME when the WAN — and thus Cloudflare DNS and Tailscale's coordination
# server — is down. Public DNS answers *.rosemaryacres.com with the Tailscale
# IP (100.x); with no internet, that name can't even be resolved and the
# Tailscale path may be down. This resolver answers rosemaryacres.com (and all
# subdomains) with the LAN IP where nginx lives, and forwards everything else
# upstream. A LAN client can still reach it with the WAN dead, so hostnames
# keep resolving locally.
#
# How it reaches clients — the Askey/Spectrum router quirk (verified 2026-07-06
# with `dhcp-probe`): the router ALWAYS hands clients itself (192.168.1.1) as
# their DNS, regardless of the Spectrum app's "DNS" field. That app field is
# the router's *upstream* — where 192.168.1.1 forwards to. So we don't point
# clients here directly; we point the app's DNS at this box and the router
# forwards the whole LAN's queries to us. (A router SPOF is accepted; it has to
# route anyway.) IPv6 is uncontrolled on that router (no v6 DNS field) and still
# hands out the ISP's v6 resolvers — v6 queries leak past split-horizon but fall
# back to this v4 path in an outage; acceptable for now.
#
# Rollout / validation (no client change needed to test):
#   1. Set the Spectrum app primary DNS = 192.168.1.65 (this host), secondary =
#      a public resolver (e.g. 1.1.1.1) as a safety net.
#   2. From any host:
#        dig @192.168.1.1 keys.rosemaryacres.com +short   -> must be 192.168.1.65
#        dig @192.168.1.1 google.com            +short    -> must still resolve
#   3. Re-check what the router hands clients any time:  sudo dhcp-probe
#
# Reusable / redundancy: import this same module on a second always-on box (the
# planned dedicated SBC, or wallace) and set it as the app's secondary, so
# neither resolver is a single point of failure. gromit is NOT router-grade
# reliable, so once the SBC lands it should become the app PRIMARY and gromit
# fall back to secondary.
{ config, lib, pkgs, ... }:

let
  # Where *.rosemaryacres.com resolves for LAN clients (nginx reverse proxy).
  proxyIP = "192.168.1.65";

  # The isolated Kindle segment (see ../kindle-segment.nix). Kept in sync with
  # the subnet defined there.
  kindleSegment = "192.168.50.0/24";
  kindleGateway = "192.168.50.1";

  # Upstreams for everything that ISN'T a local override.
  upstreams = [ "1.1.1.1" "9.9.9.9" ];

  # Prometheus metrics + REST API — loopback only (scraped by monitoring.nix).
  metricsAddr = "127.0.0.1:4000";

  # Non-disruptive probe of what the router actually hands clients (DHCP option
  # 6): sends a DISCOVER, takes NO lease. Used to verify DNS handout after a
  # router change. The matching passwordless-sudo grant is in agent/sudo.nix.
  dhcpProbe = pkgs.writeShellScriptBin "dhcp-probe" ''
    exec ${pkgs.nmap}/bin/nmap --script broadcast-dhcp-discover -e enp3s0
  '';
in
{
  services.blocky = {
    enable = true;
    settings = {
      ports = {
        dns = 53;
        http = metricsAddr;
      };

      upstreams.groups.default = upstreams;

      # Split-horizon: rosemaryacres.com AND all its subdomains resolve to the
      # LAN proxy. filterUnmappedTypes keeps AAAA (and other unmapped types) for
      # our domain from leaking upstream, where they'd get the public Tailscale
      # IP instead.
      customDNS = {
        customTTL = "1m";
        filterUnmappedTypes = true;
        mapping."rosemaryacres.com" = proxyIP;

        # The Kindle's connectivity check, answered locally instead of by
        # Amazon — see ../kindle-segment.nix for the full reasoning. Verified
        # on a live blocky 0.29: customDNS BEATS the allowlist-only denylist,
        # so these resolve here while everything else on that segment still
        # returns 0.0.0.0.
        # ⚠️ GLOBAL, not per-client: house clients get 192.168.50.1 for these
        # too, which is unroutable for them. No Amazon-OUI device was in the
        # house ARP table when this was added, but that is evidence, not proof.
        mapping."dns.kindle.com" = kindleGateway;
        mapping."spectrum.s3.amazonaws.com" = kindleGateway;
        mapping."dogvgb9ujhybx.cloudfront.net" = kindleGateway;
        mapping."kindle-wifi-cn.amazon.cn" = kindleGateway;

        # The Kindle's clock was 22 months slow, which makes every TLS
        # handshake fail "certificate not yet valid". Its own time hosts are
        # load-balanced AWS endpoints that answer differently per resolver, so
        # they are pointed at Cloudflare's stable anycast time service instead.
        # NTP has no hostname validation, so redirecting the name is fine.
        mapping."kindle-time.amazon.com" = "162.159.200.123";
        mapping."ntp-g7g.amazon.com" = "162.159.200.123";
      };

      caching = {
        minTime = "2m";
        maxTime = "30m";
        prefetching = true;
      };

      # ── The Kindle segment: allowlist-only (see ../kindle-segment.nix) ──────
      # Layer 2 of a fail-closed pair. The firewall is the real enforcement —
      # this is here so that a DNS answer alone can never hand that segment a
      # route to anywhere unintended.
      #
      # Deny-by-default via a /.*/ regex denylist, then ONE permitted name. If
      # the allowlist entry is ever wrong, nothing resolves on that segment at
      # all — including the jailbreak host — which is the correct direction to
      # fail when the device behind it has one shot.
      #
      # Verified behaviourally on blocky 0.29.0 rather than assumed, because
      # "valid config" is not "does what I think". A live instance with this
      # exact shape answered sanctuary.skyvincent.com → 78.24.205.144 while
      # returning 0.0.0.0 for google.com, amazon.com, softwareupdates.amazon.com,
      # updates.amazon.com and prod.ota-cloudfront.net. CIDR client matching was
      # confirmed the same way (a 127.0.0.0/8 group applied to a 127.0.0.1
      # client), which is what lets ONE blocky serve the house normally and this
      # segment restrictively.
      #
      # `default: []` is load-bearing: every other client on the LAN keeps
      # completely unfiltered resolution. No house-wide blocking.
      blocking = {
        denylists.everything = [
          ''
            /.*/
          ''
        ];
        # Resolve-but-do-not-route, on purpose (2026-11-01). The AP asked for
        # unifi.broadlinc.com four times and got 0.0.0.0, so it never sent an
        # inform packet — which means we still do not know which PORT it uses.
        # tcp/8080 is the UniFi convention, but convention is precisely what
        # this segment exists to not rely on. Letting the name resolve while
        # the firewall stays shut makes the AP dial for real; the
        # kindle-fw-drop log then names the ports, and only then do the
        # pinholes get written.
        #
        # Safe to resolve: the firewall is the enforcement layer and its
        # pinholes are scoped per SOURCE address, so this grants the Kindle
        # nothing — it cannot reach that host whatever DNS says.
        #
        # (The comment lives out here rather than inside the '' string because
        # entries in that string are written verbatim into blocky's list file,
        # and whether its parser honours a leading # is not something worth
        # assuming.)
        allowlists.kindle-permitted = [
          ''
            sanctuary.skyvincent.com
            kindlemodding.org
            unifi.broadlinc.com
          ''
        ];
        clientGroupsBlock = {
          default = [ ];
          "${kindleSegment}" = [ "everything" "kindle-permitted" ];
        };
        blockType = "zeroIp";
      };
      prometheus = {
        enable = true;
        path = "/metrics";
      };

      # Query logging to the journal, so `kindle-watch` can show what the
      # isolated segment ASKED FOR, per client IP. This is the half that names
      # a controller hostname like unifi.broadlinc.com; the kernel's
      # kindle-fw-drop lines are the half that catches a hardcoded IP.
      # Verified on a live blocky 0.29: a blocked query logs as
      #   queryLog: ... client_ip=… question_name=unifi.broadlinc.com.
      #   response_reason=BLOCKED (ALLOWLIST ONLY) response_type=BLOCKED
      # console = stdout = journald, so no extra log file to rotate.
      queryLog.type = "console";

      log.level = "info";
    };
  };

  # LAN-only: the router (192.168.1.1) forwards to us over enp3s0. Deliberately
  # NOT opened on tailscale0 — a remote Tailscale client must not get the LAN-IP
  # answer (it can't reach 192.168.1.65); it keeps using public DNS -> the
  # Tailscale IP. (List merges with networking.nix's per-interface allowlist.)
  networking.firewall.interfaces."enp3s0" = {
    allowedTCPPorts = [ 53 ];
    allowedUDPPorts = [ 53 ];
  };

  # Same opening on enp6s0u1, the standby USB NIC (see networking.nix). This is
  # the rule that matters most in a NIC swap: DNS here serves the whole LAN via
  # the router's forwarder, so losing :53 does not merely degrade gromit — it
  # takes name resolution away from every device in the house, which presents as
  # "the internet is down" rather than as a gromit fault. Still interface-scoped
  # rather than global, preserving the deliberate split-horizon above:
  # tailscale0 must NOT get the LAN-IP answer.
  networking.firewall.interfaces."enp6s0u1" = {
    allowedTCPPorts = [ 53 ];
    allowedUDPPorts = [ 53 ];
  };

  environment.systemPackages = [ dhcpProbe ];
}
