# Audiobookshelf audiobook / podcast server.
{ config, lib, pkgs, ... }:

{
  services.audiobookshelf = {
    enable = true;
    group = "media";
    host = "0.0.0.0";
  };
}
