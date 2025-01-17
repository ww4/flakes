# Edit this configuration file to define what should be installed on
# your system.  Help is available in the configuration.nix(5) man page
# and in the NixOS manual (accessible by running ‘nixos-help’).

{ config, pkgs, lib, ... }:

{
  imports =
    [ # Include the results of the hardware scan.
      ./hardware-configuration.nix
      # Include Nextcloud config
      ./nextcloud.nix
      # Include Immich config
      # ./immich.nix  #WIP
      # VS Code server module
      (fetchTarball {
        url = "https://github.com/nix-community/nixos-vscode-server/tarball/master";
        sha256 = "09j4kvsxw1d5dvnhbsgih0icbrxqv90nzf0b589rb5z6gnzwjnqf";
      })

    ];

  # Bootloader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot/efi";

  # Disable the GNOME3/GDM auto-suspend feature that cannot be disabled in GUI!
  # If no user is logged in, the machine will power down after 20 minutes.
  systemd.targets.sleep.enable = false;
  systemd.targets.suspend.enable = false;
  systemd.targets.hibernate.enable = false;
  systemd.targets.hybrid-sleep.enable = false;

  services.xserver.displayManager.gdm.autoSuspend = false;
  security.polkit.extraConfig = ''
    polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.login1.suspend" ||
            action.id == "org.freedesktop.login1.suspend-multiple-sessions" ||
            action.id == "org.freedesktop.login1.hibernate" ||
            action.id == "org.freedesktop.login1.hibernate-multiple-sessions")
        {
            return polkit.Result.NO;
        }
    });
  '';

# FILESYSTEMS

  fileSystems = {
  # Physical drives
      "/mnt/decom/D1" = {  # was mergerD1
        device = "/dev/disk/by-uuid/4731e760-fe51-48ef-8e35-5b764b84c249";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/decom/D2" = {  # was mergerD2
        device = "/dev/disk/by-uuid/d49dac75-4d28-4c56-b2d2-606b3271b9b5";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/decom/D3" = {  # was mergerD3
        device = "/dev/disk/by-uuid/a500d06c-9878-4db3-9873-d0ee7f07dfc0";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/decom/D4" = { # was mergerD4
        device = "/dev/disk/by-uuid/f11eeedc-60aa-4567-948a-c19ff0ccf337";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/backup/D1" = { # was mergerD6
        device = "/dev/disk/by-uuid/c9e23776-811b-4dec-9fc5-3e55454a21f0";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/backup/D2" = { # was mergerD7
        device = "/dev/disk/by-uuid/5411719d-176a-4e59-8ec2-c4efef9d5410";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/backup/D3" = { # was mergerD8
        device = "/dev/disk/by-uuid/4b986746-3de1-49de-8c02-850adcb9024e";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/backup/D4" = { # was mergerD9
        device = "/dev/disk/by-uuid/11841ae6-181e-4345-ab89-bea16dcdcc59";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

       "/mnt/primary/D1" = { #was mergerD10 / Superbook
        device = "/dev/disk/by-uuid/8976631f-34b3-4192-b979-4012a66f233f";
        fsType = "xfs";
        options = [
          "nofail"
        ];
      };

       "/mnt/primary/D2" = { # was mergerD11
        device = "/dev/disk/by-uuid/14559789-ecec-41e6-aef1-c4c445c07193";
        fsType = "xfs";
        options = [
          "nofail"
        ];
      };

       "/home/chris" = {
        device = "/dev/disk/by-uuid/56c90b01-5f1e-4058-a2c4-c3db4df4deef";
        fsType = "ext4";
      };
  
  # mergerfs: using 3 buckets, rsynced, then remove #3 for archival storage.
  
    "/mnt/fusion" = {   # Primary Bucket - 16.4 TB (contains 8TB + 10TB drive)
      device = "/mnt/primary/D*";
      fsType = "fuse.mergerfs";
      options = [
        "defaults"
        "allow_other"
        "use_ino"
        "cache.files=off"
        "moveonenospc=true"
        "dropcacheonclose=true"
        "category.create=mfs"
        "nofail"
      ];
    };  

     "/mnt/backup/all" = {   # Backup Bucket - 22 TB (contains 4x 6TB drives)
      device = "/mnt/backup/D*";
      fsType = "fuse.mergerfs";
      options = [
        "defaults"
        "allow_other"
        "use_ino"
        "cache.files=off"
        "moveonenospc=true"
        "dropcacheonclose=true"
        "category.create=mfs"
        "nofail"
      ];
    };  

     "/mnt/decom/all" = {   # Decom Bucket - 10.8 TB of soon-to-be decommisioned drives. Remove this group once done.
      device = "/mnt/decom/D*";
      fsType = "fuse.mergerfs";
      options = [
        "defaults"
        "allow_other"
        "use_ino"
        "cache.files=off"
        "moveonenospc=true"
        "dropcacheonclose=true"
        "category.create=mfs"
        "nofail"
      ];
    };  
  };


  networking.hostName = "gromit"; # Define your hostname.
  # networking.wireless.enable = true;  # Enables wireless support via wpa_supplicant.

  # Configure network proxy if necessary
  # networking.proxy.default = "http://user:password@proxy:port/";
  # networking.proxy.noProxy = "127.0.0.1,localhost,internal.domain";


  # Enable networking
  networking.networkmanager.enable = true;

  # Disable Network Manager Wait (issue on 11/3/23)
  systemd.services.NetworkManager-wait-online.enable = lib.mkForce false;
  systemd.services.systemd-networkd-wait-online.enable = lib.mkForce false;

 # Enable Tailscale
  services.tailscale.enable = true;
  networking.firewall.checkReversePath = "loose";

  # Open firewall
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [ 80 443 3000 8096 2283 9090 ];
    allowedUDPPortRanges = [
      { from = 2000; to = 4007; }
      { from = 8000; to = 8300; }
    ];
  };


  # Set your time zone.
  time.timeZone = "America/New_York";

  # Select internationalisation properties.
  i18n.defaultLocale = "en_US.UTF-8";

  i18n.extraLocaleSettings = {
    LC_ADDRESS = "en_US.UTF-8";
    LC_IDENTIFICATION = "en_US.UTF-8";
    LC_MEASUREMENT = "en_US.UTF-8";
    LC_MONETARY = "en_US.UTF-8";
    LC_NAME = "en_US.UTF-8";
    LC_NUMERIC = "en_US.UTF-8";
    LC_PAPER = "en_US.UTF-8";
    LC_TELEPHONE = "en_US.UTF-8";
    LC_TIME = "en_US.UTF-8";
  };

  # Enable the X11 windowing system.
  services.xserver.enable = true;
  
  # Enable the GNOME Desktop Environment.
  services.xserver.displayManager.gdm.enable = true;
  services.xserver.desktopManager.gnome.enable = true;

  # Configure keymap in X11
  services.xserver.xkb = {
    layout = "us";
    variant = "";
    options = "caps:super";
  };

  # Enable CUPS to print documents.
  services.printing.enable = true;

  # Enable sound with pipewire.
  # sound.enable = true; # Deprecated as of 10/15/24?
  security.rtkit.enable = true;
  services = {
    pulseaudio.enable = false;
    pipewire = {
      enable = true;
      alsa.enable = true;
      alsa.support32Bit = true;
      pulse.enable = true;
      # If you want to use JACK applications, uncomment this
      #jack.enable = true;

      # use the example session manager (no others are packaged yet so this is enabled by default,
      # no need to redefine it in your config for now)
      #media-session.enable = true;
    };
  };

  # Enable touchpad support (enabled default in most desktopManager).
  # services.xserver.libinput.enable = true;

  # Define a user account. Don't forget to set a password with ‘passwd’.
  users.users.chris = {
    isNormalUser = true;
    description = "chris";
    extraGroups = [ "networkmanager" "wheel" "media"];
    packages = with pkgs; [
      firefox
    #  thunderbird
    ];
  };
  # Add Media group for Jellyfin etc
  users.groups.media = {};
  
  # Enable automatic login for the user.
  services.displayManager.autoLogin.enable = true;
  services.displayManager.autoLogin.user = "chris";

  # Workaround for GNOME autologin: https://github.com/NixOS/nixpkgs/issues/103746#issuecomment-945091229
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;

  # Allow unfree packages
  nixpkgs.config.allowUnfree = true;

  # Allow insecure packages (added 11/3/23 for error updating)

  nixpkgs.config.permittedInsecurePackages = [
    "electron-20.3.11"
    "electron-27.3.11"
  ];

  # List packages installed in system profile. To search, run:
  # $ nix search wget 
 environment.systemPackages = with pkgs; [
  #  vim # Do not forget to add an editor to edit configuration.nix! The Nano editor is also installed by default.
  # GUI Applications
     google-chrome
     vscode
  #  teams   # deprecated, unmaintained by upstream
     logseq
  #   bitwarden  # having issues triggering a build from scratch, which fails. Not really needed...
     element-desktop
     libreoffice-fresh
     gimp       # gimp-with-plugins giving issues as of 1/8/25, switched to GIMP instead
     vlc
     feishin
     qbittorrent

  # Terminal Utilities
     byobu
     wget
     tmux
     htop
     git
     mergerfs
     tailscale
     lf
     yt-dlp
     xfsprogs
     ntfs3g
     ncdu
     gparted
     mergerfs-tools
     neofetch
     fastfetch
     bsdgames  # Colossal Cave Adventure and others
     frotz    # for infocom / zork
     # uudeview # for infocom / zork - broken as of 1/8/25
     
    ];

  # virtualbox
  virtualisation.virtualbox.host.enable = true;
  users.extraGroups.vboxusers.members = [ "chris" ];

  # Steam
  programs.steam = {
  enable = true;
  remotePlay.openFirewall = true; # Open ports in the firewall for Steam Remote Play
  dedicatedServer.openFirewall = true; # Open ports in the firewall for Source Dedicated Server
};

  # List services that you want to enable:

  # VSCode
  services = {
    vscode-server.enable = true;
  
    #OpenSSH
    openssh.enable = true;

    # Immich
    immich = {
      enable = false;  # WIP
      port = 2283;
    # mediaLocation = "/mnt/fusion/immich";
    # environment.IMMICH_MACHINE_LEARNING_URL = "http://localhost:3003";
      host = "0.0.0.0";
    };

    # Tandoor
    tandoor-recipes = {
      enable = true;
      address = "0.0.0.0";
    };

    # Jellyfin
    jellyfin = {
      enable = true;
      group = "media";
    };

    # AudioBookShelf
    audiobookshelf = {
      enable = true;
      group = "media";
      host = "0.0.0.0";
    };

    # Bitcoin Core
    bitcoind.bitcoin = {
      enable = true;
      prune = 5000;
      dbCache = 8000;
    };
  };
 
  # This value determines the NixOS release from which the default
  # settings for stateful data, like file locations and database versions
  # on your system were taken. It‘s perfectly fine and recommended to leave
  # this value at the release version of the first install of this system.
  # Before changing this value read the documentation for this option
  # (e.g. man configuration.nix or on https://nixos.org/nixos/options.html).
  system.stateVersion = "22.11"; # Did you read the comment?

  nix = {
    package = pkgs.nixVersions.stable;
    extraOptions = "experimental-features = nix-command flakes";
    optimise = {
      automatic = true;
      dates = [ "03:45" ];
      };
    gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 30d";
    };
  };
}
