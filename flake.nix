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
  };

  outputs = { self, nixpkgs, vscode-server }:
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
          ];
        };
      };
    };

}
