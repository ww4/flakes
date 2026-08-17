{
  description = "gromit — NixOS homelab server (with GNOME desktop), Home Manager + flakes";

  inputs = {
    # Pinned to nixos-26.05 stable (2026-08-15) — nixos-unstable rolled to 26.11
    # which dropped x86_64-darwin support, and nixpkgs's own module-eval touches
    # darwin option types even for a linux-only flake. Stable release still
    # supports darwin AND ships a very recent claude-code + other packages, so
    # for this box the tradeoff of "slightly less bleeding-edge" is worth it.
    nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

    # VS Code remote server support. Pinned via flake.lock instead of a
    # fetchTarball against a moving branch (which broke on every upstream push).
    vscode-server = {
      url = "github:nix-community/nixos-vscode-server";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Home Manager — release branch matching the nixos-26.05 nixpkgs pin above.
    # It tracked master from the nixpkgs-unstable era; after the 26.05 move a
    # `nix flake update` would have pulled an HM expecting newer nixpkgs than
    # ours (version-mismatch warnings, then option breakage).
    # Wired in via modules/home-manager.nix; user-level config lives in ./home.
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # comin — GitOps applier: pulls this repo and rebuilds when `main` advances
    # (i.e. when a reviewed PR is merged). See modules/agent/.
    comin.url = "github:nlewo/comin";

    # agent-modules — the scoped-agent harness (guard, hooks, managed settings),
    # extracted so gromit and the Broadlinc agent host share ONE definition
    # instead of divergent copies of a security backstop. Its `nix flake check`
    # runs a 56-case adversarial matrix against the fully rendered guard.
    agent-modules.url = "github:ww4-bot/agent-modules";
    agent-modules.inputs.nixpkgs.follows = "nixpkgs";

    # sops-nix — secrets encrypted in this repo, decrypted at activation with
    # gromit's SSH host key. See modules/sops.nix and ./.sops.yaml.
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Declarative disk partitioning — used by the wallace host's install.
    disko = {
      url = "git+https://github.com/nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Pinned 25.05 nixpkgs — ONLY for the T480 fingerprint flake below. Its
    # python-validity daemon predates the nixpkgs Python "pyproject" change and
    # won't build against unstable's stricter Python builder. Building that one
    # self-contained daemon against 25.05 (where it's known-good) and injecting
    # it into marcus's otherwise-unstable system is the least-invasive fix.
    nixpkgs-2505.url = "github:nixos/nixpkgs/nixos-25.05";

    # ThinkPad T480 fingerprint reader (06cb:009a) — used only by the marcus
    # host. The upstream flake is a nixpkgs fork that adds the sensor's package +
    # module; pin its 25.05 release branch and point it at nixpkgs-2505 (NOT our
    # unstable nixpkgs) so python-validity builds.
    nixos-06cb-009a-fingerprint-sensor = {
      url = "github:ahbnr/nixos-06cb-009a-fingerprint-sensor?ref=25.05";
      inputs.nixpkgs.follows = "nixpkgs-2505";
    };
  };

  outputs = { self, nixpkgs, nixpkgs-2505, vscode-server, home-manager, comin, agent-modules, sops-nix, disko, nixos-06cb-009a-fingerprint-sensor }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;
    in {
      nixosConfigurations = {
        # gromit — the storage + services host (the original box). Holds the
        # mergerfs media/backup pools and all data-resident services.
        gromit = lib.nixosSystem {
          inherit system;
          modules = [
            ./configuration.nix
            vscode-server.nixosModules.default
            home-manager.nixosModules.home-manager
            comin.nixosModules.comin
            agent-modules.nixosModules.agent
            sops-nix.nixosModules.sops
          ];
        };

        # wallace — the compute node (Ryzen 9 5900X / RX 580), dual-boot with
        # Windows. Bootstrap config for now; heavy CPU/GPU loads land here per
        # the wallace/gromit split. No media pool yet — drives stay on gromit.
        wallace = lib.nixosSystem {
          inherit system;
          modules = [
            disko.nixosModules.disko
            comin.nixosModules.comin
            agent-modules.nixosModules.agent
            ./hosts/wallace/disko.nix
            ./hosts/wallace/hardware-configuration.nix
            ./hosts/wallace/configuration.nix
            ./modules/agent/comin.nix   # GitOps applier — builds nixosConfigurations.wallace
          ];
        };

        # marcus — ThinkPad T480 laptop (chris + mary), intermittently online.
        # KDE Plasma 6 + Hyprland; T480 fingerprint unlock. Joined the fleet
        # 2026-06-24 (was its own nixpkgs-25.05 flake); now rides unstable and
        # applies `main` via comin whenever it's online. The fingerprint module
        # is wired only here. home-manager + the agent modules are imported from
        # ./hosts/marcus/configuration.nix.
        marcus = lib.nixosSystem {
          inherit system;
          modules = [
            comin.nixosModules.comin
            agent-modules.nixosModules.agent
            home-manager.nixosModules.home-manager
            nixos-06cb-009a-fingerprint-sensor.nixosModules."06cb-009a-fingerprint-sensor"
            ./hosts/marcus/configuration.nix
          ];
        };
      };
    };

}
