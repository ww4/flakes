{
  description = "NixOS workstation configuration using Home Manager and Flakes";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };
      lib = nixpkgs.lib;
    in {
      nixosConfigurations = {
        gromit = lib.nixosSystem {
          inherit system;
          modules = [ ./configuration.nix ];
        };
      };
    };

}
