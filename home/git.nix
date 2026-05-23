# Git. Writes ~/.config/git/config (XDG location); per-repo .git/config
# overrides still win (e.g. the flake repo's user.email = no-reply for GitHub).
{ ... }:

{
  programs.git = {
    enable = true;

    userName  = "Chris";
    userEmail = "chris@saenzmail.net";

    extraConfig = {
      # Cache HTTPS push credentials (already in use; preserve behavior).
      credential.helper = "store";
      # Default to "main" on new repos.
      init.defaultBranch = "main";
      # Explicit merge on pull. Override per-repo with `git config pull.rebase true`
      # if a project's workflow prefers rebase.
      pull.rebase = false;
    };
  };
}
