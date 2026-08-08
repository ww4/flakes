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
      name = "dc-login";
      title = "DigitalCore — sign in to keep the account";
      message = "DC disables accounts after 90 days without a sign-in. Tap, log in, done.";
      url = "https://digitalcore.club/";
      # Monthly, not daily: the only requirement is *a* sign-in inside a 90-day
      # window, so once a month clears it with 3x margin while staying quiet.
      # The 1st is deliberate — an easy-to-recognise date, and Persistent=true
      # means a reboot-straddled 1st still fires late rather than being skipped.
      calendar = "*-*-01 09:00:00";
      tags = "floppy_disk,key";
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
