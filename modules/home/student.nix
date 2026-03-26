{ config, pkgs, ... }:

{
  imports = [
    ./modules/git.nix
  ];
  
  home.username = "student";
  home.homeDirectory = "/home/student";
  home.stateVersion = "25.11";
  programs.home-manager.enable = true;

  # Preferred defaults for the user
  home.sessionVariables = {
    EDITOR = "micro";
    VISUAL = "micro";

    SSH_ASKPASS = "";
    GIT_ASKPASS = "";
  };

  # micro: pleasant defaults (mouse, softwrap, sane tab width)
  home.file.".config/micro/settings.json".text = builtins.toJSON {
    autosu = true;
    colorscheme = "default";
    mouse = true;
    tabsize = 2;
    tabstospaces = true;
    softwrap = true;
    ruler = true;
    statusline = true;
  };
}

