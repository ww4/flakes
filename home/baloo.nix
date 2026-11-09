# Baloo — KDE's file indexer — off, declaratively.
#
# On 2026-10-12 `~/.local/share/baloo` was **27 GB**. It is pure regenerable
# cache (the file-content index), so deleting it loses nothing — but
# `modules/desktop.nix` enables Plasma 6, and the indexer is started on every
# graphical login, so a delete on its own just restarts the clock. The 27 GB
# comes back. Hence: turn the indexer off in config, THEN delete.
#
# The off-switch is the autostart condition in the shipped unit, not a NixOS
# option (there isn't one). From the installed
# `baloo-6.26.0/etc/xdg/autostart/baloo_file.desktop`, verbatim:
#
#   X-KDE-autostart-condition=baloofilerc:Basic Settings:Indexing-Enabled:true
#
# i.e. baloo_file autostarts iff `baloofilerc`'s `[Basic Settings]`
# `Indexing-Enabled` is true, and the trailing `true` is the default used when
# the key is ABSENT — which is why a box with no baloofilerc still indexes.
# Writing the key false is the whole fix.
#
# Why Home-Manager and not `environment.etc."xdg/baloofilerc"`: kconfig
# cascades, and the per-user file beats a system one, so a pre-existing
# `~/.config/baloofilerc` saying true would silently win and the change would
# be a no-op that looks like a success. HM owns the file the daemon actually
# reads. `backupFileExtension = "hm-backup"` (modules/home-manager.nix) moves
# any existing file to `baloofilerc.hm-backup` rather than clobbering it.
# chris is the only user with a Plasma session; the `claude` agent user is
# headless and autostarts nothing.
#
# ⚠️ TRADE-OFF, deliberate: this file becomes a read-only symlink into the Nix
# store, so System Settings → Search → "File Search" and `balooctl6 enable`
# can no longer write it — the GUI toggle goes inert. That is the point (the
# index cannot quietly return), but re-enabling file search now means editing
# this file and rebuilding.
{ ... }:

{
  xdg.configFile."baloofilerc".text = ''
    [Basic Settings]
    Indexing-Enabled=false
  '';
}
