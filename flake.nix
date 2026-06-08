{
  description = "NixOS workstation configuration using Home Manager and Flakes";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

    # VS Code remote server support. Pinned via flake.lock instead of a
    # fetchTarball against a moving branch (which broke on every upstream push).
    vscode-server = {
      url = "github:nix-community/nixos-vscode-server";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Home Manager — tracks nixpkgs-unstable on master to match our nixpkgs.
    # Wired in via modules/home-manager.nix; user-level config lives in ./home.
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # comin — GitOps applier: pulls this repo and rebuilds when `main` advances
    # (i.e. when a reviewed PR is merged). See modules/agent/.
    comin.url = "github:nlewo/comin";
  };

  outputs = { self, nixpkgs, vscode-server, home-manager, comin }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;
    in {
      nixosConfigurations = {
        gromit = lib.nixosSystem {
          inherit system;
          modules = [
            ./configuration.nix
            vscode-server.nixosModules.default
            home-manager.nixosModules.home-manager
            comin.nixosModules.comin
          ];
        };
      };
    };

}
