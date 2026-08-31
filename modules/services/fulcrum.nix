# Fulcrum — fast Electrum protocol server. Indexes the bitcoind chain so the
# mempool.space backend (and Electrum clients like Sparrow) can query it.
#
# NOTE: this nixpkgs has NO `services.fulcrum` module — only the `fulcrum`
# package — so this is a hand-rolled systemd unit. Built on bitcoind running
# with txindex=1 (see bitcoind.nix). First-time index build takes ~6–12 h on
# top of an already-synced chain; subsequent startups are seconds. Final
# on-disk index ~100 GB under /var/lib/fulcrum (nvme root has ~329 GB free).
#
# Scope: tcp (50001) only, which is what the mempool backend uses. The TLS
# port for Sparrow-over-Tailscale (ssl 50002 + self-signed cert) is a later
# add-on — out of scope for the mempool bring-up.
{ config, lib, pkgs, ... }:

let
  # ⚠️ DERIVED, never hardcoded. This said /mnt/fusion/bitcoind/.cookie until
  # 2026-08-31, when the datadir moved to /mnt/scratch (flakes #214) and these
  # two modules were missed. The failure mode is the dangerous kind: the OLD
  # datadir still exists and still holds a stale .cookie, so this would have
  # read a plausible-looking credential that bitcoind no longer accepts, and
  # 401'd silently. That exact fault ran for 17 days in August 2026 before
  # anyone noticed. Reading it from config means the path cannot drift again.
  bitcoindCookie = "${config.services.bitcoind.bitcoin.dataDir}/.cookie";
  dataDir = "/var/lib/fulcrum";
  # Fulcrum reads a simple `key = value` config file.
  fulcrumConf = pkgs.writeText "fulcrum.conf" ''
    datadir = ${dataDir}
    bitcoind = 127.0.0.1:8332
    rpccookie = ${dataDir}/.cookie
    # tcp binds 0.0.0.0 so the mempool backend container can reach it via the
    # docker host gateway (172.17.0.1:50001); firewalled to tailscale0 + docker
    # bridges only (below) — never the LAN or the internet.
    tcp = 0.0.0.0:50001
    # fast-sync trades RAM for speed during the initial index build.
    fast-sync = 4000
  '';
in
{
  users.users.fulcrum = {
    isSystemUser = true;
    group = "fulcrum";
    home = dataDir;
  };
  users.groups.fulcrum = { };

  systemd.services.fulcrum = {
    description = "Fulcrum Electrum server";
    wantedBy = [ "multi-user.target" ];
    after = [ "bitcoind-bitcoin.service" ];
    requires = [ "bitcoind-bitcoin.service" ];
    # bitcoind regenerates its .cookie on every restart, so restart fulcrum
    # with it — the cookie-stage ExecStartPre then picks up the fresh cookie.
    partOf = [ "bitcoind-bitcoin.service" ];
    unitConfig.RequiresMountsFor = "/mnt/fusion";
    serviceConfig = {
      User = "fulcrum";
      Group = "fulcrum";
      StateDirectory = "fulcrum";
      StateDirectoryMode = "0750";
      # bitcoind writes its .cookie 0600 chris:users, which the fulcrum user
      # can't read — stage a fulcrum-owned copy as root before each start, so a
      # bitcoind restart's fresh cookie is always picked up.
      #
      # ⚠️ This USED to be `until [ -f <cookie> ]; do sleep 2; done` + install.
      # That waits for the cookie to EXIST, not to be CURRENT — and on a
      # simultaneous boot the file already exists (bitcoind's PREVIOUS one), so
      # the guard returned instantly and we staged a DEAD password. Cost:
      # 2026-07-23 → 08-09, 17 days of silent breakage — bitcoind logging
      # "incorrect password attempt", Fulcrum never opening :50001, mempool's
      # API 500ing — with every unit reporting healthy the entire time.
      #
      # Now: re-read and re-stage the cookie until bitcoind actually ACCEPTS it.
      # The HTTP status is the discriminator, and 401 is the ONLY code meaning
      # "wrong cookie":
      #   401 → stale cookie (bitcoind rotated it) → re-copy and retry
      #   000 → RPC not listening yet → retry
      #   *   → AUTH SUCCEEDED. 503 ("Loading block index") and 500 both prove
      #         the credential is good; bitcoind is merely warming up, which is
      #         Fulcrum's job to wait through, not ours. Treating those as
      #         failure would deadlock startup on a slow chain load.
      ExecStartPre = [
        ''+${pkgs.writeShellScript "fulcrum-cookie" ''
          set -eu
          for _ in $(${pkgs.coreutils}/bin/seq 1 150); do
            if [ -f ${bitcoindCookie} ]; then
              ${pkgs.coreutils}/bin/install -o fulcrum -g fulcrum -m 0400 \
                ${bitcoindCookie} ${dataDir}/.cookie
              code=$(${pkgs.curl}/bin/curl -sS -o /dev/null -w '%{http_code}' \
                --max-time 5 --user "$(${pkgs.coreutils}/bin/cat ${dataDir}/.cookie)" \
                --data-binary '{"jsonrpc":"1.0","id":"probe","method":"uptime","params":[]}' \
                -H 'content-type: text/plain;' http://127.0.0.1:8332/ 2>/dev/null || echo 000)
              case "$code" in
                401|000) ;;
                *) exit 0 ;;
              esac
            fi
            ${pkgs.coreutils}/bin/sleep 2
          done
          echo "fulcrum-cookie: bitcoind never accepted the cookie after 5 min" >&2
          exit 1
        ''}''
      ];
      # The probe loop can run up to 5 min; the default 90s start timeout would
      # kill it mid-wait and mask the very failure it exists to prevent.
      TimeoutStartSec = "600s";
      ExecStart = "${pkgs.fulcrum}/bin/Fulcrum ${fulcrumConf}";
      Restart = "on-failure";
      RestartSec = 30;
      # Indexing is I/O + CPU heavy for hours; be a polite background citizen.
      Nice = 10;
      IOSchedulingClass = "idle";
    };
  };

  # Tailscale + docker bridges only — never the LAN, never the internet.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 50001 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 50001 -j nixos-fw-accept
  '';
}
