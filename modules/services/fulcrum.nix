# Fulcrum — fast Electrum protocol server. Indexes the bitcoind chain so the
# mempool.space backend (and Electrum clients like Sparrow) can query it.
#
# NOTE: this nixpkgs has NO `services.fulcrum` module — only the `fulcrum`
# package — so this is a hand-rolled systemd unit. Built on bitcoind running
# with txindex=1 (see bitcoind.nix). First-time index build takes ~6–12 h on
# top of an already-synced chain; subsequent startups are seconds.
#
# ⚠️ SIZE, measured — and the reason this no longer lives on root. This header
# said "Final on-disk index ~100 GB under /var/lib/fulcrum (nvme root has
# ~329 GB free)" from the day it was written until 2026-10-21. The real figure
# was 183 GB on 10-12 and 184 GB on 10-21: 83% over, 41% of a 449 GB disk, and
# nothing was watching it. On 2026-10-21 root reached 100% (382 MB free).
# postgresql PANICked on ENOSPC in the checkpointer and hit its start limit,
# taking paperless-task-queue with it; Fulcrum was killed mid-commit and
# corrupted its own RocksDB past repair — "we cannot undo the inconsistent
# state ... Please delete the datadir and resynch to bitcoind". The 6–12 h
# reindex above stopped being a thing we were choosing and became a thing we
# were handed.
#
# So the datadir is not on root any more. It is a bind mount onto the Mushkin
# (see dataDir below), which makes the containment structural instead of
# vigilant: a runaway index now fills a 917 GB disk that nothing else boots
# from. The watching half is in sentinel.nix — the >90% condition had been
# detected 10,965 consecutive times and reported exactly once.
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
  # ⚠️ The PATH is unchanged; the DISK underneath it is not. /var/lib/fulcrum is
  # now a bind mount of /home/chris/fulcrum, which lives on sde1 — the Mushkin
  # Reactor 1 TB SATA SSD (SMART health PASSED, Percentage Used Endurance
  # Indicator 1, i.e. ~99% of its write budget unused), 917 GB with 616 GB free.
  #
  # Not /mnt/scratch: that is a WDC WD30EZRX, rotational, 5400 rpm. Fulcrum is a
  # RocksDB index serving small random reads for every mempool.space page load
  # and every Sparrow address sync — measured 24 h averages, nvme 122 read IOPS
  # at 48 KB mean request vs sdd 39 IOPS at 133 KB: the first random, the second
  # sequential. Not Wallace: it has a free 512 G NVMe, but Fulcrum needs
  # bitcoind's cookie and RPC, so it would trade a local socket for a tailnet
  # hop on every block.
  #
  # WHY A BIND, and not simply dataDir = "/home/chris/fulcrum": /home/chris is
  # mode 0700 chris:users, so the fulcrum user (uid 972) cannot traverse it and
  # would be refused before it ever opened the database. Path resolution starts
  # at the mount point, so the bind sidesteps the 0700 without loosening
  # permissions on a person's home directory. It also keeps this file's three
  # coupled facts — the path, the mount, and RequiresMountsFor — in one place,
  # which is the lesson of #214: that datadir move left five stale references in
  # two other modules precisely because the path lived apart from its users.
  #
  # The bind SOURCE is state, not configuration. /home/chris/fulcrum must exist
  # (0750 fulcrum:fulcrum) before this can mount, the same way /mnt/primary/D*
  # expect their filesystems to exist. It is created once during the cutover; a
  # systemd.tmpfiles rule would be ordered after local-fs.target and so would
  # lose the race to the mount it is supposed to prepare.
  #
  # Not in restic, automatically: homelab.backup.paths is an allowlist naming
  # specific /home/chris subdirectories and `fulcrum` is not one of them. A
  # regenerable 184 GB index does not belong in a snapshot, and an allowlist
  # means nobody has to remember that.
  dataDir = "/var/lib/fulcrum";
  # ⚠️ fast-sync 4000 → 1000, and the number went DOWN on purpose. The reindex
  # forced on 2026-10-21 is an initial build, so this option matters again for
  # the first time since the module was written — and its documented downside is
  # precisely the failure we just had.
  #
  # `fast-sync` is Fulcrum's older name for the UTXO cache: `fast-sync`,
  # `utxo-cache` and `utxo_cache` are all present in the 1.12.0.1 binary, while
  # the shipped example config documents only `utxo_cache`. The trade-off, from
  # that config verbatim: "a sync that is less resilient to crashes mid-sync. If
  # the process is killed mid-sync, the database may become corrupt and lose
  # UTXO data. Use this feature only if you are 100% sure that won't happen
  # during a sync."
  #
  # We are not 100% sure. Two things kill it mid-sync, and only one is fixed.
  # ENOSPC is gone — the datadir now has 616 GB under it instead of 24. OOM is
  # not: gromit has 30 G RAM with 13 G available and swap at 8.8 G of 8.8 G,
  # i.e. no headroom left at all (sentinel's swap-headroom has been firing).
  # Asking for a 4 GB cache in that state, held for 6–12 h, risks an OOM-kill
  # whose penalty is starting the entire reindex over.
  #
  # 1000 keeps most of the benefit — the docs put the point where it "really
  # pays off" at ~500 MB — for a quarter of the exposure. The option has no
  # effect once the index is built, so this is a one-time cost either way, and
  # raising it again is a one-line revert if the sync proves painfully slow.
  #
  # Fulcrum reads a simple `key = value` config file.
  fulcrumConf = pkgs.writeText "fulcrum.conf" ''
    datadir = ${dataDir}
    bitcoind = 127.0.0.1:8332
    rpccookie = ${dataDir}/.cookie
    # tcp binds 0.0.0.0 so the mempool backend container can reach it via the
    # docker host gateway (172.17.0.1:50001); firewalled to tailscale0 + docker
    # bridges only (below) — never the LAN or the internet.
    tcp = 0.0.0.0:50001
    # fast-sync trades RAM for speed during the initial index build. 1000, not
    # 4000 — see the note above fulcrumConf for why the number went DOWN.
    fast-sync = 1000
  '';
in
{
  # ⚠️ uid/gid PINNED, same reason as bitcoind.nix: the 191 GB index was copied
  # byte-for-byte to another host and carries numeric ownership 972:965. An
  # unpinned system user gets a different id there and Fulcrum cannot read its
  # own RocksDB — recovery is a 6-12 h reindex.
  users.users.fulcrum = {
    isSystemUser = true;
    uid = 972;
    group = "fulcrum";
    home = dataDir;
  };
  users.groups.fulcrum.gid = 965;

  systemd.services.fulcrum = {
    description = "Fulcrum Electrum server";
    wantedBy = [ "multi-user.target" ];
    after = [ "bitcoind-bitcoin.service" ];
    requires = [ "bitcoind-bitcoin.service" ];
    # bitcoind regenerates its .cookie on every restart, so restart fulcrum
    # with it — the cookie-stage ExecStartPre then picks up the fresh cookie.
    partOf = [ "bitcoind-bitcoin.service" ];

    # ⚠️ partOf ALONE LEAVES FULCRUM DOWN, and it did exactly that on
    # 2026-09-02. partOf propagates a STOP; nothing propagates the START back.
    # When comin deployed #227 at 07:43 it stopped fulcrum as a dependent,
    # restarted bitcoind, and never brought fulcrum back — so the Electrum
    # server sat inactive with mempool logging ECONNREFUSED until a human
    # noticed. `systemctl --failed` stayed clean the whole time, because
    # STOPPED is not FAILED: the quiet failure mode, not the loud one.
    #
    # wantedBy is NOT the fix and was already present — it only covers boot,
    # and at the 2026-08-30 reboot fulcrum did try to start and failed on the
    # bitcoind dependency, then stayed down. Upholds= (systemd 249+, we run
    # 260) is the one that means what we actually want: while bitcoind is
    # active, systemd continuously ensures fulcrum is started, including after
    # a partOf-induced stop. Safe against spinning — fulcrum already has
    # Restart=on-failure with RestartSec=30.
    upheldBy = [ "bitcoind-bitcoin.service" ];

    # ⚠️ Was "/mnt/fusion" — a stranded reference from the 2026-08-31 datadir
    # move (#214). Fulcrum's own data is /var/lib/fulcrum and the only thing it
    # needs from a mount is bitcoind's cookie, which now lives on /mnt/scratch.
    # Gating on /mnt/fusion meant a media-pool problem could block the Electrum
    # server for no reason. Derived from config so it cannot drift again.
    #
    # ⚠️ dataDir IS NOW IN THIS LIST, AND THAT LINE IS THE WHOLE GUARD. sde1 is
    # mounted `nofail` (modules/storage.nix — right for a home directory, so it
    # does not block boot on different hardware). Without this, a missed mount
    # means the bind does not exist, StateDirectory= cheerfully creates an empty
    # /var/lib/fulcrum on ROOT, and Fulcrum starts a 6–12 h reindex that fills
    # the disk we just moved it off. With it, systemd refuses to start the
    # service until var-lib-fulcrum.mount is up: the loud failure instead of the
    # quiet one. This is the hazard written up in
    # docs/fulcrum-relocation-test-2026-10-12.md as "finding 2", nine days
    # before the quiet version of it happened for an unrelated reason.
    #
    # DEMONSTRATED, not assumed, because it is fair to doubt: `nofail` makes
    # fstab-generator attach the mount to local-fs.target with Wants= rather
    # than Requires=, so does the service-side dependency survive it? Yes —
    # there is already a live instance of this exact pattern on gromit.
    # /mnt/scratch is `options = [ "nofail" ]` (modules/storage.nix) and
    # bitcoind-bitcoin has RequiresMountsFor on a path beneath it:
    #
    #   $ systemctl show bitcoind-bitcoin -p Requires -p After --value \
    #       | tr ' ' '\n' | grep mount
    #   -.mount
    #   mnt-scratch.mount      <- Requires=
    #   mnt-scratch.mount      <- After=
    #
    # It lands in BOTH despite the nofail. So boot is not blocked when the disk
    # is absent, and Fulcrum refuses to start instead of silently reindexing
    # onto root — which is the whole point.
    unitConfig.RequiresMountsFor = [
      config.services.bitcoind.bitcoin.dataDir
      dataDir
    ];
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
              # ⚠️ WHITELIST, not a blacklist — full account in services/mempool.nix.
              # Short version: this was `401|000) wait ;; *) succeed`, and
              # `curl -w '%{http_code}' ... || echo 000` yields "000000" when
              # bitcoind is not listening yet (curl's -w prints 000, then the ||
              # appends a second). "000000" is neither 401 nor 000, so the old
              # blacklist read a REFUSED CONNECTION AS SUCCESS and Fulcrum started
              # against the previous boot's cookie — 401 forever, the Electrum port
              # never opened, and mempool dead behind it (2026-09-08).
              #
              # Only 200/500/503 prove auth actually succeeded. Everything else,
              # known or not, waits.
              code=$(${pkgs.curl}/bin/curl -sS -o /dev/null -w '%{http_code}' \
                --max-time 5 --user "$(${pkgs.coreutils}/bin/cat ${dataDir}/.cookie)" \
                --data-binary '{"jsonrpc":"1.0","id":"probe","method":"uptime","params":[]}' \
                -H 'content-type: text/plain;' http://127.0.0.1:8332/ 2>/dev/null) || true
              case "''${code:-000}" in
                200|500|503) exit 0 ;;
                *) ;;   # 401 wrong cookie, 000 not listening, anything else unknown
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

  # The datadir's actual home. Deliberately HERE and not in modules/storage.nix:
  # that file is for hardware — physical devices and the mergerfs pools built
  # out of them, where its own comment says member mounts stay with disko. This
  # is not a disk, it is one service's state redirected onto another disk, and
  # it is meaningless apart from the dataDir and the RequiresMountsFor above.
  # Splitting them is how #214 produced five stale paths in two other modules.
  #
  # `nofail` is correct and is NOT a loosening: it keeps a missing bind from
  # blocking boot, while RequiresMountsFor above keeps Fulcrum from starting
  # without it. Boot survives; the Electrum server stays down and says so.
  #
  # `depends` makes the ordering explicit rather than relying on systemd
  # inferring it from the path prefix: this cannot mount until the filesystem
  # holding its source is up.
  # `mkDefault` on the two host-specific values only. The path, the mount and
  # RequiresMountsFor still live together in this file — that is the #214
  # lesson above and it is unchanged. What varies between hosts is solely
  # WHERE the bytes sit: gromit kept them on sde1 under /home/chris; wallace
  # has them on its Intel NVMe. A host overrides those two and inherits every
  # guard (nofail here, RequiresMountsFor on dataDir above) without copying it.
  fileSystems."/var/lib/fulcrum" = {
    device = lib.mkDefault "/home/chris/fulcrum";
    fsType = "none";
    options = [ "bind" "nofail" ];
    depends = lib.mkDefault [ "/home/chris" ];
  };

  # Tailscale + docker bridges only — never the LAN, never the internet.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 50001 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 50001 -j nixos-fw-accept
  '';
}
