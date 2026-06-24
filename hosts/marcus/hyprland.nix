# Hyprland desktop for chris@marcus — beginner-friendly, fully declarative
# (Home-Manager). Goal: at least as usable as Plasma for getting work done,
# but lighter and slicker. KDE Plasma 6 stays installed as a fallback session
# you can pick at the SDDM login screen anytime.
#
# What this wires up:
#   - Hyprland compositor config (keybinds, input, looks)   — this file
#   - Waybar      top bar (workspaces, clock, tray, net, battery, volume, brightness)
#   - wofi        app launcher (Super+D / Super+Space)
#   - mako        notifications
#   - hyprlock    lock screen   + hypridle (auto-lock / DPMS / suspend)
#   - hyprpaper   wallpaper (solid Nord background via swaybg, can't break)
#   - kitty       terminal (Super+Return); Konsole/Dolphin from KDE still work
#   - grim/slurp/swappy screenshots; cliphist clipboard history; udiskie automount;
#     polkit-gnome auth agent (GUI password prompts)
#
# The system-level `programs.hyprland.enable` (in configuration.nix) provides the
# session, portals and the SDDM entry; here we set `package = null` so HM only
# manages the *config* and reuses that system Hyprland (no second copy / no skew).
{ config, lib, pkgs, ... }:
let
  # Foolproof wallpaper: a solid Nord-dark fill via swaybg — no image file to
  # go missing. Swap for an image later (hyprpaper) once you have one you like.
  bgColor = "2e3440";
in
{
  home.packages = with pkgs; [
    kitty            # terminal
    swaybg           # wallpaper (solid colour)
    pavucontrol      # GUI volume mixer (Waybar volume click target)
    wofi             # launcher (also in system list; harmless)
    brightnessctl    # backlight keys
    playerctl        # media keys
    grim slurp swappy  # screenshots
    cliphist         # clipboard history
    wl-clipboard     # wl-copy / wl-paste
    udiskie          # USB automount tray
    libnotify        # notify-send (used by some keybinds/scripts)
  ];

  # --- Cursor + GTK/icon theme (consistent look across GTK apps) ---
  home.pointerCursor = {
    gtk.enable = true;
    package = pkgs.bibata-cursors;
    name = "Bibata-Modern-Classic";
    size = 24;
  };
  gtk = {
    enable = true;
    theme = { name = "Nordic"; package = pkgs.nordic; };
    iconTheme = { name = "Papirus-Dark"; package = pkgs.papirus-icon-theme; };
  };
  # KDE's appearance settings rewrite these GTK files as real files, so HM's
  # backup-on-activation keeps colliding with a stale *.hm-backup from the first
  # run — which FAILS the whole activation (blocking every comin deploy). HM owns
  # these declaratively; force-overwrite instead of backing up. (Cosmetic
  # side-effect: GTK apps use the Nordic theme inside KDE sessions too.)
  gtk.gtk2.force = true;                                  # ~/.gtkrc-2.0 (module's own option)
  xdg.configFile."gtk-3.0/settings.ini".force = true;
  xdg.configFile."gtk-4.0/settings.ini".force = true;

  # --- Terminal (sane defaults so Super+Return "just works") ---
  programs.kitty = {
    enable = true;
    settings = {
      font_family = "monospace";
      font_size = 11;
      background_opacity = "0.95";
      confirm_os_window_close = 0;
      scrollback_lines = 10000;
    };
  };

  # --- Hyprland compositor ---
  wayland.windowManager.hyprland = {
    enable = true;
    package = null;        # reuse the system Hyprland (programs.hyprland.enable)
    portalPackage = null;  # portals come from the system module too
    xwayland.enable = true;
    systemd.enable = true; # binds graphical-session.target so user services start

    # Write hyprland.conf (hyprlang), which the system Hyprland reads. As of HM
    # with home.stateVersion >= 26.05 the configType default FLIPPED to "lua"
    # (writes hyprland.lua, which a standard Hyprland ignores) — our shared home
    # pins 26.05, so without this the entire config silently never applied.
    configType = "hyprlang";

    settings = {
      # Laptop display: let Hyprland auto-pick the preferred mode. Bump the last
      # number to 1.25/1.5 if you want HiDPI scaling on the T480 panel.
      monitor = ",preferred,auto,1";

      "$mod" = "SUPER";
      "$terminal" = "kitty";
      "$fileManager" = "dolphin";
      "$menu" = "wofi --show drun";

      # Autostart: bar, notifications, wallpaper, clipboard watcher, automount,
      # polkit agent (GUI auth prompts). NOTE: hypridle is intentionally NOT here
      # — services.hypridle (below) runs it as a systemd user service bound to
      # graphical-session.target; listing it here too would start a second daemon.
      exec-once = [
        "waybar"
        "mako"
        "swaybg -m solid_color -c \"#${bgColor}\""
        "wl-paste --watch cliphist store"
        "udiskie --tray"
        "nm-applet --indicator"   # NetworkManager tray applet — manage/reconnect wifi from Hyprland
        "${pkgs.polkit_gnome}/libexec/polkit-gnome/polkit-gnome-authentication-agent-1"
      ];

      env = [
        "XCURSOR_SIZE,24"
        "NIXOS_OZONE_WL,1"
      ];

      input = {
        kb_layout = "us";
        follow_mouse = 1;
        sensitivity = 0;
        touchpad = {
          natural_scroll = true;
          tap-to-click = true;
          disable_while_typing = true;
        };
      };

      general = {
        gaps_in = 5;
        gaps_out = 10;
        border_size = 2;
        "col.active_border" = "rgba(88c0d0ee) rgba(5e81acee) 45deg";
        "col.inactive_border" = "rgba(2e3440aa)";
        layout = "dwindle";
        resize_on_border = true;
      };

      decoration = {
        rounding = 8;
        blur = {
          enabled = true;
          size = 6;
          passes = 2;
        };
        shadow = {
          enabled = true;
          range = 12;
          render_power = 3;
        };
      };

      animations = {
        enabled = true;
        bezier = [ "ease, 0.25, 0.1, 0.25, 1.0" ];
        animation = [
          "windows, 1, 4, ease, slide"
          "windowsOut, 1, 4, ease, slide"
          "border, 1, 8, ease"
          "fade, 1, 5, ease"
          "workspaces, 1, 4, ease, slide"
        ];
      };

      dwindle = {
        pseudotile = true;
        preserve_split = true;
      };

      misc = {
        disable_hyprland_logo = true;
        force_default_wallpaper = 0;
      };

      gestures.workspace_swipe = true;

      # --- Keybindings (Super-based, discoverable) ---
      bind = [
        "$mod, Return, exec, $terminal"
        "$mod, Q, killactive"
        "$mod, E, exec, $fileManager"
        "$mod, D, exec, $menu"
        "$mod, Space, exec, $menu"
        "$mod, V, togglefloating"
        "$mod, F, fullscreen"
        "$mod, P, pseudo"
        "$mod, J, togglesplit"
        "$mod SHIFT, Q, exit"            # log out of Hyprland
        "$mod, L, exec, loginctl lock-session"
        "$mod, B, exec, google-chrome-stable"

        # Move focus (arrows + vim hjkl)
        "$mod, left, movefocus, l"
        "$mod, right, movefocus, r"
        "$mod, up, movefocus, u"
        "$mod, down, movefocus, d"
        "$mod, h, movefocus, l"
        "$mod, l, movefocus, r"
        "$mod, k, movefocus, u"
        "$mod, j, movefocus, d"

        # Workspaces 1-10
        "$mod, 1, workspace, 1"
        "$mod, 2, workspace, 2"
        "$mod, 3, workspace, 3"
        "$mod, 4, workspace, 4"
        "$mod, 5, workspace, 5"
        "$mod, 6, workspace, 6"
        "$mod, 7, workspace, 7"
        "$mod, 8, workspace, 8"
        "$mod, 9, workspace, 9"
        "$mod, 0, workspace, 10"

        # Move active window to workspace
        "$mod SHIFT, 1, movetoworkspace, 1"
        "$mod SHIFT, 2, movetoworkspace, 2"
        "$mod SHIFT, 3, movetoworkspace, 3"
        "$mod SHIFT, 4, movetoworkspace, 4"
        "$mod SHIFT, 5, movetoworkspace, 5"
        "$mod SHIFT, 6, movetoworkspace, 6"
        "$mod SHIFT, 7, movetoworkspace, 7"
        "$mod SHIFT, 8, movetoworkspace, 8"
        "$mod SHIFT, 9, movetoworkspace, 9"
        "$mod SHIFT, 0, movetoworkspace, 10"

        # Scroll through workspaces
        "$mod, mouse_down, workspace, e+1"
        "$mod, mouse_up, workspace, e-1"

        # Screenshots: region to clipboard+editor / full screen
        ", Print, exec, grim -g \"$(slurp)\" - | swappy -f -"
        "$mod, Print, exec, grim - | swappy -f -"

        # Clipboard history picker
        "$mod, C, exec, cliphist list | wofi --dmenu | cliphist decode | wl-copy"
      ];

      # Repeatable binds (held keys): volume + brightness
      bindel = [
        ", XF86AudioRaiseVolume, exec, wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%+"
        ", XF86AudioLowerVolume, exec, wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-"
        ", XF86MonBrightnessUp, exec, brightnessctl s 10%+"
        ", XF86MonBrightnessDown, exec, brightnessctl s 10%-"
      ];

      # Locked binds (work even on lock screen): mute + media
      bindl = [
        ", XF86AudioMute, exec, wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"
        ", XF86AudioMicMute, exec, wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle"
        ", XF86AudioPlay, exec, playerctl play-pause"
        ", XF86AudioNext, exec, playerctl next"
        ", XF86AudioPrev, exec, playerctl previous"
      ];

      # Mouse: Super+LMB move, Super+RMB resize
      bindm = [
        "$mod, mouse:272, movewindow"
        "$mod, mouse:273, resizewindow"
      ];
    };
  };

  # --- Waybar top bar ---
  programs.waybar = {
    enable = true;
    settings.mainBar = {
      layer = "top";
      position = "top";
      height = 32;
      spacing = 6;
      modules-left = [ "hyprland/workspaces" "hyprland/window" ];
      modules-center = [ "clock" ];
      modules-right = [ "tray" "pulseaudio" "backlight" "network" "battery" ];

      "hyprland/workspaces" = {
        on-click = "activate";
        format = "{id}";
      };
      "hyprland/window" = {
        max-length = 60;
        separate-outputs = true;
      };
      clock = {
        format = "{:%a %d %b  %H:%M}";
        tooltip-format = "<tt><small>{calendar}</small></tt>";
      };
      pulseaudio = {
        format = "{volume}% {icon}";
        format-muted = "muted ";
        format-icons.default = [ "" "" "" ];
        on-click = "pavucontrol";
        scroll-step = 5;
      };
      backlight = {
        format = "{percent}% {icon}";
        format-icons = [ "" "" "" ];
        on-scroll-up = "brightnessctl s 5%+";
        on-scroll-down = "brightnessctl s 5%-";
      };
      network = {
        format-wifi = "{essid} ({signalStrength}%) ";
        format-ethernet = "wired ";
        format-disconnected = "offline ";
        tooltip-format = "{ifname}: {ipaddr}";
        on-click = "kitty -e nmtui";
      };
      battery = {
        states = { warning = 30; critical = 15; };
        format = "{capacity}% {icon}";
        format-charging = "{capacity}% ";
        format-icons = [ "" "" "" "" "" ];
      };
      tray = { spacing = 10; };
    };
    style = ''
      * {
        font-family: "JetBrainsMono Nerd Font", "Noto Sans", sans-serif;
        font-size: 13px;
      }
      window#waybar {
        background: rgba(46, 52, 64, 0.92);
        color: #d8dee9;
      }
      #workspaces button {
        padding: 0 8px;
        color: #d8dee9;
        background: transparent;
      }
      #workspaces button.active {
        background: #5e81ac;
        color: #eceff4;
        border-radius: 6px;
      }
      #clock, #pulseaudio, #backlight, #network, #battery, #tray {
        padding: 0 10px;
      }
      #battery.warning  { color: #ebcb8b; }
      #battery.critical { color: #bf616a; }
    '';
  };

  # --- Notifications ---
  services.mako = {
    enable = true;
    settings = {
      background-color = "#2e3440";
      text-color = "#d8dee9";
      border-color = "#5e81ac";
      border-radius = 8;
      default-timeout = 6000;
      font = "Noto Sans 11";
    };
  };

  # --- Lock screen ---
  programs.hyprlock = {
    enable = true;
    settings = {
      general = {
        hide_cursor = true;
        grace = 2;          # 2s grace so a stray keypress doesn't force re-auth
      };
      background = [{
        color = "rgba(46, 52, 64, 1.0)";
      }];
      input-field = [{
        size = "260, 50";
        position = "0, -60";
        halign = "center";
        valign = "center";
        outline_thickness = 2;
        fade_on_empty = true;
        placeholder_text = "<i>Password…</i>";
      }];
      label = [{
        text = "$TIME";
        font_size = 56;
        position = "0, 120";
        halign = "center";
        valign = "center";
      }];
    };
  };

  # --- Idle behaviour: lock at 5m, screen off at 6m, suspend at 12m ---
  services.hypridle = {
    enable = true;
    settings = {
      general = {
        lock_cmd = "pidof hyprlock || hyprlock";
        before_sleep_cmd = "loginctl lock-session";
        after_sleep_cmd = "hyprctl dispatch dpms on";
      };
      listener = [
        { timeout = 300; on-timeout = "loginctl lock-session"; }
        { timeout = 360; on-timeout = "hyprctl dispatch dpms off"; on-resume = "hyprctl dispatch dpms on"; }
        { timeout = 720; on-timeout = "systemctl suspend"; }
      ];
    };
  };

  # --- Launcher styling (wofi) ---
  programs.wofi.enable = true;
}
