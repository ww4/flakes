# daily-reminders — dumb, deliberately un-clever daily nudges over ntfy.
#
# Each entry becomes one systemd service + timer that sends a single tappable
# ntfy notification. It does NOT log into anything, claim anything, or scrape
# anything — the whole point is that a human taps the link and does the thing.
# That matters for sites (private trackers especially) whose daily-reward and
# streak systems exist precisely to reward genuine presence: automating the
# claim is what gets accounts banned, so this automates the *remembering*
# instead, which is the part that was actually annoying.
#
# To add a reminder: append to `reminders` below and flake-PR. Fields:
#   name     unit-name suffix (reminder-<name>.{service,timer})
#   title    ntfy notification title
#   message  body text
#   url      tapping the notification opens this (ntfy "Click:" header)
#   time     systemd OnCalendar time-of-day, local (gromit runs America/New_York)
#   tags     ntfy tags/emoji, comma-separated
#   calendar OPTIONAL full OnCalendar expression, for anything not daily. When
#            set it replaces the daily default entirely and `time` is ignored.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  reminders = [
    {
      name = "retrotoon";
      title = "Retrotoon daily bonus";
      message = "Claim today's reward and keep the streak alive.";
      url = "https://retrotoon.world/";
      # 09:00 local = 13:00 UTC. Reward days almost always roll at 00:00 UTC,
      # so a fixed local time lands exactly one claim per reward-day with a
      # wide margin either side of the boundary. Change freely; just avoid
      # ~19:30-20:30 local, which straddles the UTC rollover.
      time = "09:00";
      tags = "film_projector,gift";
    }
    {
      name = "dc-bonus";
      title = "DigitalCore daily bonus";
      message = "Claim today's bonus points and keep the streak going.";
      url = "https://digitalcore.club/";
      # 09:05 rather than 09:00 purely so this and the Retrotoon nudge arrive as
      # two distinguishable notifications instead of a simultaneous pair. Same
      # rollover caveat as above: avoid ~19:30-20:30 local.
      time = "09:05";
      tags = "floppy_disk,gift";
    }
    {
      name = "dc-login";
      title = "DigitalCore — sign in to keep the account";
      message = "DC disables accounts after 90 days without a sign-in. Tap, log in, done.";
      url = "https://digitalcore.club/";
      # Kept even though `dc-bonus` above now nudges daily, because the two
      # guard different stakes: missing a daily bonus costs some points, missing
      # a sign-in for 90 days costs the ACCOUNT. If the daily nudge ever starts
      # being ignored (the likely failure mode for a habit reminder) this is the
      # backstop that still fires. Drop it only if the daily one proves sticky.
      # Monthly, not daily: the only requirement is *a* sign-in inside a 90-day
      # window, so once a month clears it with 3x margin while staying quiet.
      # The 1st is deliberate — an easy-to-recognise date, and Persistent=true
      # means a reboot-straddled 1st still fires late rather than being skipped.
      calendar = "*-*-01 09:00:00";
      tags = "floppy_disk,key";
    }
    {
      name = "nic-cable-test";
      title = "Test: is it the cable or the motherboard port?";
      message = ''
        gromit's enp3s0 has been stuck at 100Mbps/Full since 2026-08-25 and a new cable did not fix it.
        Move that same cable to the ASIX USB adapter (enp6s0u1) and read the speed:
          cat /sys/class/net/enp6s0u1/speed
        1000 -> cable and wall run are fine, the ONBOARD PORT is bad.
        100  -> the port is fine, it is the CABLE or the in-wall run.
        Firewall already mirrors enp3s0 onto enp6s0u1, so SSH, LAN DNS and Jellyfin-direct keep working on it.
        DELETE THIS REMINDER once the answer is known.'';
      url = "https://glances.rosemaryacres.com";
      # ⚠️ ONE-SHOT BY INTENT, unlike every other entry here. The others guard
      # recurring habits; this guards a single diagnostic that is blocked on a
      # physical event (the box reaching its permanent home), which no calendar
      # can express. Weekly rather than daily so it stays a nudge and not a nag,
      # Saturday morning because that is when there is time to move a machine.
      # It carries its own instructions so it is actionable without going back
      # through the diagnosis, and it says to delete itself — if this is still
      # firing in a month, the reminder is the thing that failed, not Chris.
      calendar = "Sat *-*-* 10:00:00";
      tags = "electric_plug,mag";
    }
    {
      name = "d6-cable-swap";
      title = "Swap the D6 USB cable (new one arrived Friday)";
      message = ''
        D6 = the WD My Book on usb 6-2. Its cable has been faulting since the move: 5,000+ USB resets and 5,000+ read errors per boot, load-dependent.

        WARNING: the masking tape on that drive says "backup". It is NOT a backup drive, it is a fusion POOL member. Do not identify a drive by its tape - match the enclosure on usb 6-2.

        1. Swap the cable.
        2. REBOOT. This is owed anyway: D6 is currently carrying a stacked double mount (the manual `-o nouuid` recovery mount sitting on top of systemd's) and a pinned superblock. Only a reboot clears both.
        3. After the reboot, verify - do not just assume it came back:
             grep -c ' /mnt/primary/D6 ' /proc/self/mountinfo   -> must be 1, not 2
             for d in 1 2 3 4 5 6; do systemctl is-active mnt-primary-D$d.mount; done
             ls /mnt/primary/D6/    (a readdir is the ONLY thing that detects a zombie mount)
        4. Did it work? Compare against the baseline, do not eyeball it:
             journalctl -k -b | grep -c 'reset SuperSpeed USB device'
             journalctl -k -b | grep -c 'I/O error, dev sdm'
           Before the swap: 5,394 resets and 5,281 read errors in one boot, still climbing under load. After: should be at or near zero. Put some read load on the pool first, since the fault only shows under I/O.
        5. Then resume the 124 paused public torrents in qBittorrent. They were stopped 2026-08-25 to take read load off D6. The 41 private ones were never stopped.

        DELETE THIS REMINDER once the cable is swapped and the counts are clean.'';
      url = "https://glances.rosemaryacres.com";
      # ⚠️ ONE-SHOT BY INTENT, same as nic-cable-test above. Chris ordered the
      # cable 2026-08-26 for Friday delivery and asked to be reminded Saturday.
      # 10:05 rather than 10:00 so it does not land in the same instant as the
      # NIC test and get read as one notification — they are separate jobs.
      #
      # It carries the whole procedure because the parts that are easy to get
      # wrong are not the swap itself: the tape says "backup" on a pool drive,
      # the verification needs a readdir rather than df/mountpoint, and "it
      # mounted fine" is not proof while a stacked mount can still be present.
      # The before-numbers are inlined so the after-check has something to be
      # measured against — otherwise "seems better" is the only verdict
      # available, and that is how the quiet fault stayed invisible for 10 h.
      calendar = "Sat *-*-* 10:05:00";
      tags = "floppy_disk,electric_plug";
    }
  ];

  # ⚠️ The message goes through a FILE, not the command line.
  #
  # systemd rejects a raw newline inside a unit-file value. Passing a multi-line
  # message straight to ExecStart produced a unit that looked completely normal
  # in `systemctl cat` but that systemd refused to load:
  #
  #     LoadState=bad-setting
  #     LoadError=... "has a bad unit file setting."
  #     systemctl show -p ExecStart  ->  empty
  #     the timer's NextElapseUSecRealtime -> empty, i.e. it would NEVER fire
  #
  # That is exactly what happened to reminder-nic-cable-test (added 2026-08-25,
  # flakes #189): it sat there for a week looking installed and could never have
  # fired. A reminder that cannot fire is worse than no reminder, because it is
  # being relied on. Single-line reminders were unaffected, which is why nothing
  # showed up until a multi-line one was added.
  #
  # Writing the body to the store and cat-ing it at runtime makes the class of
  # bug impossible rather than relying on every future message staying on one
  # line. Note `systemctl status` alone would NOT have caught this either —
  # only LoadState/LoadError and the empty NextElapse did.
  mkService = r:
    let
      msgFile = pkgs.writeText "reminder-${r.name}.txt" r.message;
      runner = pkgs.writeShellScript "reminder-${r.name}" ''
        exec ${gromit-notify}/bin/gromit-notify \
          ${lib.escapeShellArg r.title} \
          "$(cat ${msgFile})" \
          default \
          ${lib.escapeShellArg r.tags} \
          ${lib.escapeShellArg r.url}
      '';
    in
    lib.nameValuePair "reminder-${r.name}" {
      description = "Reminder: ${r.title}";
      # Notification-only: if ntfy is down there is nothing to retry and no state
      # to corrupt, so a failed run is a genuine failed unit (surfaces in /health).
      serviceConfig = {
        Type = "oneshot";
        # "default" priority above is deliberate and must stay — a reminder must
        # never bypass quiet hours.
        ExecStart = runner;
      };
    };

  mkTimer = r: lib.nameValuePair "reminder-${r.name}" {
    description = "Reminder timer: ${r.title}";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = r.calendar or "*-*-* ${r.time}:00";
      # If gromit was down at the scheduled time, still nudge once on boot —
      # a missed reminder is the one failure mode that defeats the purpose.
      Persistent = true;
    };
  };
in
{
  systemd.services = lib.listToAttrs (map mkService reminders);
  systemd.timers = lib.listToAttrs (map mkTimer reminders);
}
