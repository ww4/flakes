# GYB ("Got Your Back") — incremental Gmail backups.
#
# The archive lives at /home/chris/gyb/GYB-GMail-Backup-<account>/ and is
# covered by restic (see backup.nix).
#
# ONE-TIME SETUP — interactive, run as chris, before the timer can work:
#   gyb --action create-project --email <account>      # creates the API project
#   gyb --email <account> --action backup \            # first run = OAuth grant
#       --local-folder /home/chris/gyb/GYB-GMail-Backup-<account>
# Once oauth2.txt exists in each folder, the nightly timer runs unattended.
{ config, lib, pkgs, ... }:

let
  accounts = [ "driveonwood@gmail.com" "kymetro9999@gmail.com" ];
  gybRoot = "/home/chris/gyb";
in
{
  environment.systemPackages = [ pkgs.gyb ];

  systemd.services.gyb-backup = {
    description = "GYB — incremental Gmail backup";
    onFailure = [ "notify-failure@%N.service" ];
    serviceConfig = {
      Type = "oneshot";
      User = "chris";
      Group = "users";
    };
    script = ''
      rc=0
      for acct in ${lib.concatStringsSep " " accounts}; do
        echo "GYB backup: $acct"
        ${pkgs.gyb}/bin/gyb --email "$acct" --action backup \
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
