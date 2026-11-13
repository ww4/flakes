# craigmyle-link-watch — watch the Craigmyle 60 GHz link directly, from here.
#
# THE QUESTION THIS ANSWERS, in Chris's words: "what we're watching for is how
# often the rebooter has to fire". A ConnectSense Rebooter Pro power-cycles the
# office end of the AF60 link when it loses the far side.
#
# ⭐ TWO PROBES SEPARATE THE TWO CAUSES, which nothing else we tried could:
#
#   .156 office radio unreachable   -> its POWER was cut. The rebooter fired.
#   .220 far radio unreachable
#        while .156 is up           -> the RF path dropped. Rain, most likely.
#
# 60 GHz is severely rain-attenuated and a cloudburst dropping the link for a
# few minutes is EXPECTED behaviour, not a fault. Only the first line is a
# firing, and a firing is ~75 s of .156 being dark (10 s off_duration + ~65 s
# boot and relock, both measured 2026-11-04 and again on 11-11 and 11-12).
#
# WHY NOT THE OBVIOUS ALTERNATIVES, each tried and rejected on evidence:
#   - The device's own push callback. Registered three times, including with
#     the maintained client's payload shape; it has never sent a single packet,
#     not even a TCP SYN, across three confirmed reboots. Its local API is four
#     endpoints and carries NO history - the app's "last rebooted" comes from
#     the vendor's cloud.
#   - Inferring duration from the camera watch. It polls every 10 minutes and
#     the thing to measure is ~11 minutes, so it catches a firing on two
#     consecutive samples only if the outage starts within ~75 s of a poll -
#     about one time in eight. It caught 11-11 and 11-12 by luck.
#
# This is only possible because of the 4via6 subnet route approved 2026-11-12;
# before that nothing at Craigmyle was reachable except the NVR.
{ config, lib, pkgs, ... }:

let
  bbPort = 9115;

  # 4via6 addresses, site ID 1. Produced by `tailscale debug via 1 <ip>/32`,
  # never converted by hand. See [[customer-lan-via-4via6]].
  via = suffix: "fd7a:115c:a1e0:b1a:0:1:${suffix}";

  targets = [
    { addr = via "c0a8:101"; name = "meraki-gateway"; role = "control"; ipv4 = "192.168.1.1"; }
    { addr = via "c0a8:19c"; name = "af60-office";    role = "near";    ipv4 = "192.168.1.156"; }
    { addr = via "c0a8:1dc"; name = "af60-new-shop";  role = "far";     ipv4 = "192.168.1.220"; }
    { addr = via "c0a8:1d9"; name = "cam-new-shop";   role = "behind";  ipv4 = "192.168.1.217"; }
  ];

  # ⚠️ ICMP, not HTTP, deliberately. Both radios answer HTTPS 200, but an HTTP
  # probe measures the radio's MANAGEMENT CPU, not its reachability - a loco M5
  # at K Family Farms once read 21/240/35 ms jitter while hosts behind it read
  # 4 ms clean. A probe that reports "down" because a web server is busy would
  # manufacture phantom rebooter firings. ICMP is also far lighter on somebody
  # else's equipment.
  bbConfig = pkgs.writeText "blackbox-craigmyle.yml" (builtins.toJSON {
    modules.icmp_v6 = {
      prober = "icmp";
      timeout = "5s";
      icmp.preferred_ip_protocol = "ip6";
    };
  });
in
{
  # ⚠️ WHAT ACTUALLY MAKES ICMP WORK HERE IS NOT WHAT THE UNIT IMPLIES.
  # The generated unit carries `AmbientCapabilities=CAP_NET_RAW` AND an empty
  # `CapabilityBoundingSet=`. An ambient capability outside the bounding set is
  # masked, so that CAP_NET_RAW is INERT. Probes succeed anyway because
  # blackbox_exporter uses unprivileged ICMP datagram sockets, which this host
  # permits: `net.ipv4.ping_group_range = 0 2147483647`, i.e. every group,
  # including the transient DynamicUser GID. Proven before writing this module
  # by running blackbox 0.28.0 as the unprivileged `claude` user against all
  # three targets - probe_success 1, no capability errors.
  # ⇒ If probes ever start failing wholesale, suspect that sysctl being
  # tightened, NOT the capability. Chasing the capability would waste the day.
  services.prometheus.exporters.blackbox = {
    enable = true;
    port = bbPort;
    listenAddress = "127.0.0.1";
    configFile = bbConfig;
  };

  services.prometheus.scrapeConfigs = [{
    job_name = "craigmyle-link";
    # 30 s: a firing is ~75 s dark, so every episode lands on at least two
    # samples. This is the resolution the 10-minute camera poll never had.
    scrape_interval = "30s";
    metrics_path = "/probe";
    params.module = [ "icmp_v6" ];
    static_configs = map (t: {
      targets = [ t.addr ];
      labels = { name = t.name; role = t.role; ipv4 = t.ipv4; site = "craigmyle"; };
    }) targets;
    relabel_configs = [
      { source_labels = [ "__address__" ]; target_label = "__param_target"; }
      { source_labels = [ "__param_target" ]; target_label = "instance"; }
      { target_label = "__address__"; replacement = "127.0.0.1:${toString bbPort}"; }
    ];
  }];

  # ⚠️ NO ALERTS HERE, on purpose. Chris already gets a push from the vendor's
  # app the moment the device reboots - it beat our own camera watch to the
  # 11-11 event - and `blueiris-watch` already pages when the nine new-shop
  # cameras drop. A third notifier for the same event is noise, and network
  # faults must never pierce quiet hours anyway. This module exists for the
  # TREND, which is the thing nobody could see. Add an alert later only if a
  # question comes up that the dashboard cannot answer.

  services.grafana.provision.dashboards.settings.providers = [{
    name = "craigmyle";
    folder = "Craigmyle";
    options.path = pkgs.runCommand "craigmyle-dashboards" { } ''
      mkdir -p $out
      ln -s ${./craigmyle-link-dashboard.json} $out/craigmyle-link.json
    '';
  }];
}
