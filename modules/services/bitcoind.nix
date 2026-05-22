# Bitcoin Core full node.
{ config, lib, pkgs, ... }:

{
  # Runs as chris against the existing ~/.bitcoin (pruned node, ~17 GB).
  # prune is also recorded in ~/.bitcoin/settings.json, which was stripped
  # of that key so this is the single source of truth.
  services.bitcoind.bitcoin = {
    enable = true;
    user = "chris";
    group = "users";
    dataDir = "/home/chris/.bitcoin";
    prune = 4768;
    dbCache = 8000;
  };
}
