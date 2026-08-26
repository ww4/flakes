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
  ];

  mkService = r: lib.nameValuePair "reminder-${r.name}" {
    description = "Reminder: ${r.title}";
    # Notification-only: if ntfy is down there is nothing to retry and no state
    # to corrupt, so a failed run is a genuine failed unit (surfaces in /health).
    serviceConfig = {
      Type = "oneshot";
      ExecStart = lib.escapeShellArgs [
        "${gromit-notify}/bin/gromit-notify"
        r.title
        r.message
        "default"       # never urgent — this must not bypass quiet hours
        r.tags
        r.url
      ];
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
