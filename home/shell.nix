# Bash. The pre-HM ~/.bashrc was Debian-flavored boilerplate (debian_chroot
# checks, /usr/bin/lesspipe, etc.) — dropped because none of it applies to
# NixOS. Existing file is preserved at ~/.bashrc.hm-backup on first switch.
{ ... }:

{
  programs.bash = {
    enable = true;

    # Bumped from 1000/2000 — disk is cheap, shell history is precious.
    historySize = 10000;
    historyFileSize = 20000;

    # ignoredups: skip lines that match the immediately previous one
    # ignorespace: skip lines starting with a space (handy for `unlock` etc.
    # if you want a credential-bearing command out of history)
    historyControl = [ "ignoredups" "ignorespace" ];

    shellOptions = [ "histappend" "checkwinsize" ];

    shellAliases = {
      # ls family
      ll = "ls -alF";
      la = "ls -A";
      l  = "ls -CF";
      ls = "ls --color=auto";
      grep  = "grep --color=auto";
      fgrep = "fgrep --color=auto";
      egrep = "egrep --color=auto";

      # Personal shortcuts
      b      = "byobu";
      sshe   = "ssh e -p 4089";
      sshn   = "ssh n";
      zork   = "frotz ZORKI";
      # Bitwarden CLI unlock — exports BW_SESSION for the current shell.
      unlock = ''bw unlock > /tmp/bw_env && eval "$(grep -o export.* /tmp/bw_env)" && bw status | grep -o "status.*"'';
    };

    sessionVariables = {
      EDITOR = "nano";
    };

    bashrcExtra = ''
      # Clean color prompt (no debian_chroot — NixOS).
      PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
      case "$TERM" in
        xterm*|rxvt*)
          PS1="\[\e]0;\u@\h: \w\a\]$PS1"
          ;;
      esac

      # `ed` opens whatever $EDITOR is currently set to. Function (not alias)
      # so the variable resolves at call time, not at shell-init time.
      ed() { "''${EDITOR:-vi}" "$@"; }
    '';
  };
}
