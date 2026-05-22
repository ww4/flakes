# GYB ("Got Your Back") — incremental Gmail backups.
#
# The archive lives at /home/chris/gyb/GYB-GMail-Backup-<account>/ and is
# covered by restic (see backup.nix).
#
# ONE-TIME SETUP — interactive, run as chris, before the timer can work.
# --config-folder is required: GYB otherwise writes credentials next to its
# own binary, which is read-only in the Nix store.
#   gyb --config-folder /home/chris/gyb --action create-project \
#       --email <account>
#   gyb --config-folder /home/chris/gyb --email <account> --action backup \
#       --local-folder /home/chris/gyb/GYB-GMail-Backup-<account>
# Once the OAuth token exists, the nightly timer runs unattended.
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
        ${pkgs.gyb}/bin/gyb --config-folder "${gybRoot}" \
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
