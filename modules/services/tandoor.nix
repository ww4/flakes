# Tandoor Recipes.
{ config, lib, pkgs, ... }:

{
  services.tandoor-recipes = {
    enable = true;
    address = "0.0.0.0";
  };
}
