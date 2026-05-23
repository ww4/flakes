# Home Manager wiring. The actual user config lives under ../home/.
#
# `useGlobalPkgs` + `useUserPackages` keep HM riding the same nixpkgs the
# system uses (no second evaluation, no duplicate downloads).
#
# `backupFileExtension` is the safety net: the first time HM tries to manage
# a file that already exists (e.g. ~/.bashrc), it moves the existing one to
# ~/.bashrc.hm-backup instead of failing or silently clobbering. Inspect and
# delete the backups once you're satisfied with HM's version.
{ ... }:

{
  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    backupFileExtension = "hm-backup";

    users.chris = import ../home;
  };
}
