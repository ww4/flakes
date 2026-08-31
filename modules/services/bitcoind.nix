# Bitcoin Core full node.
#
# Hosting Fulcrum + mempool.space requires the full (non-pruned) chain
# with txindex=1.
#
# ⚠️ THE DATADIR LIVES ON /mnt/scratch, NOT ON THE MEDIA POOL — deliberately.
# It was on /mnt/fusion until 2026-08-31. mergerfs does not drop a branch when
# its drive vanishes; it unions the bare mountpoint directory left on the root
# filesystem, so the datadir stays "present" as a partial view of itself. On
# 2026-08-27 D6 dropped, bitcoind saw blocks with no chainstate/CURRENT, LevelDB
# created a fresh database, and the real blocks/index was garbage-collected as
# obsolete. The blocks survived; the index did not, at a cost of 18-30 h of
# reindexing. D1 and D2 are still USB drives, so the exposure was ongoing.
#
# /mnt/scratch is a single direct-SATA disk (WD30EZRX, 2.7 TB free) — no union,
# no USB, and RequiresMountsFor can actually gate on it, which it never could on
# mergerfs. It also takes bitcoind's random I/O off the media pool.
{ config, lib, pkgs, ... }:

{
  # Dedicated user (2026-08 audit L6): this ran as chris, so a bitcoind bug
  # had chris's SSH keys, GPG keys and wallets in reach, and the RPC cookie
  # doubled as a chris-owned credential. The cookie consumers (fulcrum's
  # ExecStartPre and mempool-cookie-sync) both read it as root and are
  # unaffected. For manual bitcoin-cli use:
  #   sudo -u bitcoind bitcoin-cli -datadir=/mnt/scratch/bitcoind ...
  users.users.bitcoind = {
    isSystemUser = true;
    group = "bitcoind";
    home = "/mnt/scratch/bitcoind";
  };
  users.groups.bitcoind = { };

  # One-time ownership handoff, ordered before the daemon: flipping the unit
  # user without this leaves bitcoind unable to read its own chainstate and
  # crash-looping. Idempotent — a chown to the current owner is a no-op run.
  # Metadata-only, but the chainstate holds ~100k files; give it room.
  systemd.services.bitcoind-datadir-owner = {
    description = "one-time: hand the bitcoind datadir to the bitcoind user";
    before = [ "bitcoind-bitcoin.service" ];
    requiredBy = [ "bitcoind-bitcoin.service" ];
    unitConfig.RequiresMountsFor = config.services.bitcoind.bitcoin.dataDir;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      TimeoutStartSec = "30min";
    };
    script = ''
      d=${config.services.bitcoind.bitcoin.dataDir}
      if [ ! -d "$d" ]; then
        echo "datadir $d missing — refusing to invent one" >&2
        exit 1
      fi
      # Check the THING, not a proxy. The first version checked the top-level
      # dir's owner — but the bitcoind module's own tmpfiles rule chowns that
      # one directory during activation, BEFORE this unit runs, so the guard
      # said "done" on its very first invocation, the recursive chown never
      # happened, and bitcoind started as its new user unable to read its own
      # chris-owned files (2026-08-18 incident: "settings.json — please check
      # permissions", with fulcrum + mempool down as dependents). Scanning
      # for ANY entry not owned by bitcoind cannot false-positive; once the
      # tree is fully migrated the scan is one metadata sweep and no chown.
      straggler=$(${pkgs.findutils}/bin/find "$d" ! -user bitcoind -print -quit)
      if [ -n "$straggler" ]; then
        echo "handing $d contents to bitcoind (first hit: $straggler)..."
        ${pkgs.coreutils}/bin/chown -R bitcoind:bitcoind "$d"
      fi
    '';
  };

  services.bitcoind.bitcoin = {
    enable = true;
    user = "bitcoind";
    group = "bitcoind";
    dataDir = "/mnt/scratch/bitcoind";  # off the mergerfs pool — see header
    prune = 0;                          # full chain
    dbCache = 8000;                     # 8 GB chainstate cache — drops to ~450 MB after IBD
    extraConfig = ''
      server=1
      txindex=1                          # required by Fulcrum + mempool
      # ZMQ pubsub: mempool.space subscribes to these for live block/tx
      # notifications instead of polling.
      zmqpubrawblock=tcp://127.0.0.1:28332
      zmqpubrawtx=tcp://127.0.0.1:28333
      zmqpubhashblock=tcp://127.0.0.1:28334
      # RPC reachability for the mempool backend container, which connects via
      # the docker host gateway (172.17.0.1:8332). Bind all interfaces but gate
      # with rpcallowip to loopback + docker bridge subnets only. Port 8332 is
      # NOT in the firewall allowlist (allowedTCPPorts=[80 443]), so the LAN and
      # internet can't reach it regardless — rpcallowip is the second layer.
      # 0.0.0.0 (not 172.17.0.1) avoids a boot-ordering dependency on docker0
      # existing before bitcoind starts.
      rpcbind=0.0.0.0
      rpcallowip=127.0.0.1
      rpcallowip=172.16.0.0/12
      # RPC CONCURRENCY. Defaults (rpcworkqueue=16, rpcthreads=4) are sized for a
      # human with a wallet, not for mempool.space's indexer, which fans out a
      # burst of getrawtransaction calls per block. When the queue fills bitcoind
      # answers 503 and DROPS the rest — "Request rejected because http work
      # queue depth exceeded", 135 times in 20h on 2026-08-09/10. mempool's
      # $updateBlocks loop then wedges and its tip silently freezes hours behind
      # while every service still reports healthy (it fired the sentinel
      # api-content check four times: 08-09 16:23/21:41, 08-10 04:00/06:01, each
      # ~3h stale, each self-recovering, then repeating on a ~6h sawtooth).
      # A deeper queue costs memory only when actually queued; threads are idle
      # when unused. Paired with the mempool cache-permission fix in
      # services/mempool.nix — an unwritable cache is what makes mempool re-fetch
      # hard enough to saturate the queue in the first place.
      rpcworkqueue=64
      rpcthreads=8
    '';
  };

  # Now a REAL gate. On /mnt/fusion this was decorative: mergerfs stays mounted
  # when a branch dies, so RequiresMountsFor was satisfied by a union serving a
  # partial datadir. /mnt/scratch is a single filesystem, so if it is absent the
  # unit simply does not start.
  systemd.services.bitcoind-bitcoin.unitConfig.RequiresMountsFor =
    config.services.bitcoind.bitcoin.dataDir;
  systemd.services.bitcoind-bitcoin.after = [ "bitcoind-datadir-owner.service" ];
  systemd.services.bitcoind-bitcoin.requires = [ "bitcoind-datadir-owner.service" ];

  # ─── Restart policy + a fail-closed datadir guard ────────────────────────
  #
  # ⚠️ WHY: on 2026-08-27 this cost the entire block index and forced a reindex.
  #
  # D6 dropped off USB at 19:24:49. mergerfs STAYS MOUNTED when a branch
  # disappears (so RequiresMountsFor above cannot help), and D6 held the real
  # chainstate — so bitcoind saw a datadir with blocks but no `chainstate/CURRENT`.
  # It did what LevelDB does with a missing CURRENT: created a fresh, empty
  # database. Then systemd restarted it, with `RestartSec` at the 100ms default
  # and `StartLimitBurst=5` in 10s — all retries burned in **783 ms**.
  #
  # pool-autoremount had D6 back at 19:26:28, **54 seconds after bitcoind had
  # already given up**. Later rounds (triggered by comin deploys) repeated it:
  # 54 start attempts total, and LevelDB garbage-collected the real
  # `blocks/index` as obsolete against the fresh manifest. The blocks survived;
  # the index did not.
  #
  # So the restart policy did not merely fail to recover — it converted a
  # 90-second storage blip into a multi-day rebuild. Two changes:
  #
  # 1. RestartSec=180s. pool-autoremount runs every 2 min and took ~99 s here,
  #    so 3 min guarantees at least one full recovery cycle between attempts.
  #    StartLimitIntervalSec widened to 30 min so five attempts can actually
  #    space out instead of collapsing into one second.
  systemd.services.bitcoind-bitcoin.serviceConfig.RestartSec = 180;
  systemd.services.bitcoind-bitcoin.unitConfig.StartLimitIntervalSec = 1800;
  systemd.services.bitcoind-bitcoin.unitConfig.StartLimitBurst = 5;

  # 2. Fail CLOSED before bitcoind can touch a half-present datadir. The
  #    discriminating signal is exactly the one that was missing that night:
  #    `chainstate/CURRENT`. If blocks exist but that pointer does not, the
  #    datadir is incomplete — refuse to start rather than let LevelDB
  #    initialise a fresh database over it.
  #
  #    ⚠️ A legitimate `-reindex` also starts without chainstate/CURRENT, so the
  #    guard is bypassable by an explicit, deliberate marker file. It must be a
  #    conscious act, never a default.
  systemd.services.bitcoind-bitcoin.serviceConfig.ExecStartPre =
    let
      # Read from config, NOT hardcoded — the datadir is moving to SSD, and a
      # guard pointed at a stale path would silently pass on every start.
      dataDir = config.services.bitcoind.bitcoin.dataDir;
      # Every fusion branch that must be mounted before ANY verdict about the
      # datadir means anything. Derived from fileSystems, not hardcoded, so a
      # branch added later cannot be silently forgotten here.
      poolBranches = lib.filter (m: lib.hasPrefix "/mnt/primary/D" m)
        (lib.attrNames config.fileSystems);
      guard = pkgs.writeShellScript "bitcoind-datadir-guard" ''
        set -euo pipefail
        DD="${dataDir}"
        # ─── Pool integrity FIRST — before the marker, before every size test ──
        # $DD lives on a mergerfs union whose branches are GLOBBED
        # (device = "/mnt/primary/D*"). When a branch drive is unmounted,
        # mergerfs does not drop the branch — it unions the bare mountpoint
        # directory left behind on the root filesystem. The datadir therefore
        # still "exists" and is still readable, but is a partial view of
        # itself, and every size/completeness test below reads that partial
        # view as destroyed data.
        #
        # 2026-08-28: the D6 drive (WD My Book, USB) dropped and did not
        # remount. blocks/index measured 1 MB through the degraded union, so
        # the index check added the day before concluded "stub — a reindex is
        # required" and told the operator to touch the override marker. The
        # index was fine; it was on the unmounted drive. Acting on that advice
        # would have reindexed over an incomplete block set — the very
        # destruction the guard exists to prevent.
        #
        # This runs BEFORE the override marker deliberately. The marker
        # authorises a conscious -reindex; it must never authorise a start
        # against an incomplete pool, because a reindex from a partial block
        # set is itself destructive.
        case "$DD" in
          /mnt/fusion/*)
            missing=""
            for br in ${lib.concatStringsSep " " poolBranches}; do
              if ! ${pkgs.util-linux}/bin/mountpoint -q "$br"; then
                missing="$missing $br"
              fi
            done
            if [ -n "$missing" ]; then
              echo "bitcoind-guard: REFUSING — fusion pool branch(es) NOT mounted:$missing" >&2
              echo "  $DD is a mergerfs union. An unmounted branch is silently" >&2
              echo "  replaced by its bare mountpoint on the root fs, so the datadir" >&2
              echo "  reads as a partial copy of itself. Any 'index is a stub' or" >&2
              echo "  'chainstate is missing' verdict right now would be an artefact" >&2
              echo "  of the missing drive, NOT real data loss." >&2
              echo "  Do NOT touch /var/lib/bitcoind-allow-fresh for this." >&2
              echo "  Remount the drive, confirm the pool is whole, then start." >&2
              exit 1
            fi
            ;;
          /mnt/*)
            # ⚠️ THE MIRROR HAZARD, and the move to /mnt/scratch is what creates
            # it. A single-filesystem datadir has the opposite failure mode to
            # the union above: if the disk is NOT mounted, "$DD" is simply an
            # empty directory on the ROOT filesystem. Then `[ ! -d "$DD/blocks" ]`
            # below is true, the guard reports "no blocks dir — first run,
            # allowing", and bitcoind begins a fresh IBD over nothing while ~700
            # GB of real chain sits unreachable on the unmounted disk.
            #
            # Same shape as the mergerfs case, opposite cause: there a union hid
            # an absent branch, here an absent mount hides behind an empty dir.
            # Both make "the data is gone" and "I cannot see the data" produce
            # identical evidence.
            #
            # RequiresMountsFor already gates the unit. This check does not
            # depend on that: it is the fail-closed layer, and a fail-closed
            # layer that trusts another layer is not one.
            mp="/mnt/$(echo "''${DD#/mnt/}" | cut -d/ -f1)"
            if ! ${pkgs.util-linux}/bin/mountpoint -q "$mp"; then
              echo "bitcoind-guard: REFUSING — $mp is NOT MOUNTED." >&2
              echo "  $DD would be an empty directory on the root filesystem," >&2
              echo "  which is indistinguishable from a genuine first run. The" >&2
              echo "  chain is on the unmounted disk, not missing." >&2
              echo "  Do NOT touch /var/lib/bitcoind-allow-fresh for this." >&2
              echo "  Mount $mp, then start." >&2
              exit 1
            fi
            ;;
        esac
        if [ -e /var/lib/bitcoind-allow-fresh ]; then
          echo "bitcoind-guard: override marker present — allowing a fresh/reindex start"
          exit 0
        fi
        # ⚠️ MIGRATION SAFETY. "No blocks here" is a legitimate first run ONLY if
        # there is no chain anywhere. During the 2026-08-31 move off the media
        # pool there is a window where this config is deployed but the copy has
        # not been made — and in that window the branch below would report
        # "first run, allowing" and bitcoind would begin a FRESH IBD FROM GENESIS
        # while ~700 GB of real chain sat at the old path. Not destructive, but
        # it burns days and produces a second, confusing datadir.
        #
        # So: if this datadir is empty and the PREVIOUS one still holds blocks,
        # the migration is incomplete. Refuse and say so. Remove the old datadir
        # once the move is done and this check retires itself.
        OLD_DD=/mnt/fusion/bitcoind
        if [ ! -d "$DD/blocks" ] && [ "$DD" != "$OLD_DD" ] && [ -d "$OLD_DD/blocks" ]; then
          echo "bitcoind-guard: REFUSING — $DD has no blocks, but $OLD_DD does." >&2
          echo "  The datadir move is INCOMPLETE. Starting now would begin a" >&2
          echo "  fresh initial block download from genesis and ignore the" >&2
          echo "  existing chain entirely." >&2
          echo "  Copy the datadir first:  sudo bitcoind-relocate --apply" >&2
          echo "  Then start. Remove $OLD_DD only after the reindex succeeds." >&2
          exit 1
        fi
        # A datadir with no blocks at all is a legitimate first run.
        if [ ! -d "$DD/blocks" ]; then
          echo "bitcoind-guard: no blocks dir — first run, allowing"
          exit 0
        fi
        # ⚠️ FAIL CLOSED on unreadable. "cannot read" and "is empty" produce the
        # same empty string, and treating the first as the second is precisely
        # how this class of fault hides — it is the mistake that ran through the
        # whole 2026-08-27 investigation.
        if ! ls -A "$DD/blocks" >/dev/null 2>&1; then
          echo "bitcoind-guard: REFUSING — cannot read $DD/blocks (permissions or" >&2
          echo "  a dead mount). That is NOT the same as an empty datadir." >&2
          exit 1
        fi
        if [ -z "$(ls -A "$DD/blocks")" ]; then
          echo "bitcoind-guard: blocks dir empty — first run, allowing"
          exit 0
        fi
        # Blocks present but the BLOCK INDEX is missing/absurdly small => the
        # index was destroyed (2026-08-27: LevelDB GC'd it after a fresh manifest
        # took over). A real index is 150-250 MB; what was left was ~67 KB.
        # Starting here cannot succeed and only writes more junk.
        if [ ! -s "$DD/blocks/index/CURRENT" ]; then
          echo "bitcoind-guard: REFUSING — $DD/blocks exists but" >&2
          echo "  blocks/index/CURRENT is missing. The block index is gone;" >&2
          echo "  bitcoind cannot start without a -reindex." >&2
          echo "  To authorise that reindex: sudo touch /var/lib/bitcoind-allow-fresh" >&2
          exit 1
        fi
        idxsz=$(du -s --block-size=1M "$DD/blocks/index" 2>/dev/null | cut -f1)
        if [ -n "''${idxsz:-}" ] && [ "$idxsz" -lt 20 ]; then
          echo "bitcoind-guard: REFUSING — $DD/blocks/index is only ''${idxsz} MB." >&2
          echo "  A real block index is 150-250 MB. This is a stub left by a" >&2
          echo "  failed start, not a usable index. A -reindex is required." >&2
          echo "  To authorise it: sudo touch /var/lib/bitcoind-allow-fresh" >&2
          exit 1
        fi
        # Blocks present but chainstate pointer missing => incomplete datadir.
        if [ ! -s "$DD/chainstate/CURRENT" ]; then
          echo "bitcoind-guard: REFUSING TO START — $DD/blocks exists but" >&2
          echo "  chainstate/CURRENT is missing or empty. The datadir is incomplete" >&2
          echo "  (a pool branch is probably absent). Starting now would let LevelDB" >&2
          echo "  create a fresh database and garbage-collect the real one — this is" >&2
          echo "  exactly what destroyed the block index on 2026-08-27." >&2
          echo "  Fix the storage first. To force a genuine reindex:" >&2
          echo "    sudo touch /var/lib/bitcoind-allow-fresh" >&2
          exit 1
        fi
        echo "bitcoind-guard: datadir looks complete"
      '';
    in
    lib.mkBefore [ "${guard}" ];

  # The mempool backend container (on a docker bridge, e.g. mempool-net) reaches
  # bitcoind RPC via the host gateway 172.17.0.1:8332. rpcbind=0.0.0.0 + the
  # rpcallowip above are necessary but NOT sufficient: nixos-fw default-drops the
  # container's packets at the INPUT layer before rpcallowip is ever consulted,
  # so the connection silently times out (ETIMEDOUT in mempool-api). Fulcrum's
  # 50001 already has this accept; 8332 was missing it (latent until Fulcrum
  # finished indexing). Accept 8332 from docker bridges ONLY — never tailscale0
  # or the LAN; cookie auth + rpcallowip remain the gate for who can actually use
  # it. Mirrors modules/services/fulcrum.nix.
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 8332 -j nixos-fw-accept
  '';
}
