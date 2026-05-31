# GYB ("Got Your Back") — incremental Gmail backups.
#
# The archive lives at /home/chris/gyb/GYB-GMail-Backup-<account>/ and is
# covered by restic (see backup.nix).
#
# ONE-TIME SETUP — interactive, run as chris, before the timer can work.
# Each account gets its OWN config-folder so credentials/tokens don't collide;
# --config-folder is also required because the Nix store (next to the gyb
# binary, GYB's default) is read-only. Per account:
#   gyb --config-folder /home/chris/gyb/<account> --action create-project \
#       --email <account>
#   gyb --config-folder /home/chris/gyb/<account> --email <account> \
#       --action backup \
#       --local-folder /home/chris/gyb/GYB-GMail-Backup-<account>
# Once each account's OAuth token exists, the nightly timer runs unattended.
{ config, lib, pkgs, ... }:

let
  accounts = [ "driveonwood@gmail.com" "kymetro9999@gmail.com" ];
  gybRoot = "/home/chris/gyb";
in
{
  environment.systemPackages = [ pkgs.gyb ];

  systemd.services.gyb-backup = {
    description = "GYB — incremental Gmail backup";
    serviceConfig = {
      Type = "oneshot";
      User = "chris";
      Group = "users";
    };
    script = ''
      rc=0
      for acct in ${lib.concatStringsSep " " accounts}; do
        echo "GYB backup: $acct"
        ${pkgs.gyb}/bin/gyb --config-folder "${gybRoot}/$acct" \
          --email "$acct" --action backup \
          --local-folder "${gybRoot}/GYB-GMail-Backup-$acct" || rc=1
      done
      exit $rc
    '';
  };

  systemd.timers.gyb-backup = {
    description = "Nightly GYB Gmail backup";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "01:40";
      Persistent = true;
    };
  };
}
