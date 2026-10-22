# Advance PHP — and only PHP — to 8.4.27, the PHP2Shell security release.
#
# WHY THIS EXISTS. CVE-2026-109618 and CVE-2026-109619 (both CVSS 9.2) are
# UNAUTHENTICATED remote code execution in PHP itself, fixed upstream in 8.4.27
# on 2026-10-21. 109618 is corruption via PHP's out-of-memory handling, which
# longjmp()s out of a request that is only partially initialised; 109619 is
# stale global state left pointing at freed request memory and then reused by a
# LATER request on the same worker. Both fire BEFORE application code runs, so
# Nextcloud's own OIDC/Authelia login is not a control here — php-fpm is already
# executing attacker-influenced paths by the time any app code is reached.
# Exploit details leaked ahead of the patch and exploitation was reported the
# same week.
#
# This box's nixpkgs pin is from 2026-02-23 and ships php 8.4.24.
#
# BLAST RADIUS ON GROMIT: one consumer. php-fpm has a single pool (nextcloud;
# `/run/phpfpm/` holds only nextcloud.sock), serving cloud.rosemaryacres.com.
# Nothing else in the system closure instantiates php, and none of the OCI
# containers are PHP. The vhost has no auth_request in front of it, so requests
# reach php-fpm pre-auth. What limits exposure is the http-level source gate in
# nginx (allow loopback/RFC1918/100.64.0.0/10, then `deny all`), which rejects
# at the nginx layer BEFORE fastcgi_pass — so PHP never runs for a denied
# source. Reachable from the home LAN and the tailnet, not from the internet
# (public DNS points every vhost at the tailnet address; WAN 443 refuses).
# That is why this is a scoped pin and not an emergency channel bump.
#
# WHY A SECOND NIXPKGS AND NOT A version/hash OVERRIDE OF OUR OWN php84.
# The obvious move is `prev.php84.override { version = "8.4.27"; hash = …; }`,
# reusing the Feb builder. That was tried first and it DOES evaluate correctly
# — php and all ~40 extensions rebuild from the 8.4.27 tarball — but the build
# FAILS, and it fails in the worst possible place:
#
#   FAILED TEST SUMMARY
#   filter.default=encoded doesn't reach hard limit while parsing POST data
#   filter_input() doesn't reach hard limit
#   filter_input_array() doesn't reach hard limit
#
# Those are three of the six `oom_*` regression tests the security release ADDED
# for CVE-2026-109618. The three that failed are exactly the ones driven by
# request input (filter_input, filter_input_array, POST parsing); the three
# filter_var ones passed. Upstream's own nixos-26.05 php-filter-8.4.27 is
# SUBSTITUTABLE from cache.nixos.org, i.e. Hydra built it and those tests pass
# there — so 8.4.27 is fine and our eight-month-old build environment is what
# they trip over. Narrowing that down is not work worth doing to ship a security
# patch, and silencing the CVE's own regression tests to make a CVE fix build is
# the wrong trade at any price.
#
# So: take php from the release branch that built it. `nixpkgs-php` tracks
# nixos-26.05, the same release as our main pin, just current — the smallest
# possible move that still gets an upstream-TESTED interpreter. It is a plain
# package import with no module eval (the pattern homelab.silverbullet already
# uses for nixpkgs-silverbullet), so unstable's darwin eval problem that pins
# this flake to 26.05 is not in play. Cost is a parallel php closure (~138 MiB
# of downloads, no local compilation); nothing else in the system moves.
#
# WHY A VERSION FLOOR AND NOT A BARE ASSIGNMENT. modules/agent/claude-code-pin.nix
# records the trap: "an overlay is not a floor, it is an assignment", and so it
# has to carry a ⚠️ REMOVE THIS FILE note that someone must act on. A security
# pin is exactly the case where that note gets missed — and silently pinning php
# BACKWARDS off a future channel bump would reintroduce this CVE. So the
# override is guarded by `lib.versionOlder`: it applies only while the main pin
# is behind 8.4.27 and becomes a no-op the moment nixpkgs catches up. It needs
# no cleanup and cannot downgrade. Demonstrated, not assumed — evaluated against
# a stub nixpkgs at 8.4.30 whose `.override` throws: the result stays 8.4.30 and
# the override is never called.
#
# TO RETIRE: once the main nixpkgs pin advances past 8.4.27 this file stops
# doing anything. Drop it and the `nixpkgs-php` input together at that point.
{ lib, nixpkgs-php, ... }:

let
  # Plain package import. `system` is hardcoded rather than read from
  # `pkgs.stdenv.hostPlatform` on purpose: this module DEFINES an entry in
  # nixpkgs.overlays, and touching `pkgs` to build that entry is a cycle
  # (pkgs is the fixpoint the overlays feed into). The flake is x86_64-linux
  # only — flake.nix hardcodes the same `system`.
  phpPkgs = import nixpkgs-php { system = "x86_64-linux"; };
in
{
  nixpkgs.overlays = [
    # NOTE the shape: `php84` is defined UNCONDITIONALLY and only its VALUE is
    # guarded. Deciding the attribute NAME with `lib.optionalAttrs
    # (lib.versionOlder prev.php84.version …)` looks equivalent and is not — it
    # makes the overlay's key set depend on evaluating a package out of `prev`,
    # and nixpkgs' `with pkgs;` in all-packages.nix forces those names while the
    # fixpoint is still being built. That is an immediate `error: infinite
    # recursion encountered at pkgs/top-level/all-packages.nix:28` (hit here on
    # the first attempt, and the same wall others hit on the NixOS Discourse
    # thread for this CVE). Inside the value, reading prev.php84.version is fine.
    (final: prev: {
      php84 =
        if lib.versionOlder prev.php84.version "8.4.27" then phpPkgs.php84 else prev.php84;
    })
  ];
}
