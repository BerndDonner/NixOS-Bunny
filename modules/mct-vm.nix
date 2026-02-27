{ config, pkgs, lib, ... }:

let
  username = "student";
  continueConfigYaml = ''
name: Tabby-Replacement
version: 0.1.0
schema: v1

models:
  - name: DeepSeek-Autocomplete
    provider: ollama
    model: deepseek-coder:6.7b
    apiBase: http://ai.donner-lab.org:11434
    roles:
      - autocomplete
    autocompleteOptions:
      debounceDelay: 250
      maxPromptTokens: 1024
      onlyMyCode: true
    defaultCompletionOptions:
      temperature: 0.3
      stop:
        - "\n"

  - name: Qwen-Chat
    provider: ollama
    model: qwen2.5-coder:7b
    apiBase: http://ai.donner-lab.org:11434
    roles:
      - chat
      - edit
      - apply
    defaultCompletionOptions:
      temperature: 0.6
      maxTokens: 5000
      contextLength: 32000

context:
  - provider: file
  - provider: code
  - provider: diff

rules:
  - Keep answers concise and correct.
  - Prefer Python-style pseudocode when language is unclear.

data:
  - name: local-log
    destination: file://~/.continue/events
    schema: 0.2.0
    level: noCode

disableTelemetry: true
'';

  # Where VSIX files will live inside the image.
  vsixDir = "/opt/vscode-vsix";

  installExtensionsScript = pkgs.writeShellScript "mct-install-vscode-extensions" ''
    set -euo pipefail

    # Run as the logged-in user.
    marker="$HOME/.config/mct/vscode-extensions-installed"
    if [ -e "$marker" ]; then
      exit 0
    fi

    mkdir -p "$(dirname "$marker")"

    # Try to find a VS Code binary from vscode-fhs.
    CODE_BIN="${pkgs.vscode-fhs}/bin/code"
    if [ ! -x "$CODE_BIN" ]; then
      # Fallback: maybe it's called code-fhs or comes via PATH
      CODE_BIN="${pkgs.vscode-fhs}/bin/code-fhs"
    fi
    if [ ! -x "$CODE_BIN" ]; then
      CODE_BIN="$(command -v code || true)"
    fi

    if [ -z "${CODE_BIN:-}" ] || [ ! -x "${CODE_BIN:-}" ]; then
      echo "ERROR: VS Code binary not found (vscode-fhs)." >&2
      exit 1
    fi

    shopt -s nullglob
    vsixes=("${vsixDir}"/*.vsix)
    if [ "${#vsixes[@]}" -eq 0 ]; then
      echo "INFO: No VSIX files found in ${vsixDir}. Nothing to install." >&2
      : > "$marker"
      exit 0
    fi

    for v in "${vsixes[@]}"; do
      echo "Installing VSIX: $v" >&2
      "$CODE_BIN" --install-extension "$v" --force >/dev/null || {
        echo "WARN: Failed to install $v" >&2
      }
    done

    : > "$marker"
  '';

in {
  imports = [
    ./git-module.nix
  ];

  nixpkgs.config.allowUnfree = true;

  networking.hostName = "mct-vm";
  time.timeZone = "Europe/Berlin";

  # VM guest integration
  virtualisation.vmware.guest.enable = true;

  # --- Desktop: KDE Plasma (Wayland default; X11 selectable)
  services.xserver.enable = true;
  services.xserver.videoDrivers = [ "vmware" ];

  services.displayManager.sddm.enable = true;
  services.displayManager.sddm.wayland.enable = true;
  services.displayManager.defaultSession = "plasmawayland";

  services.desktopManager.plasma6.enable = true;

  # Autologin
  services.displayManager.autoLogin.enable = true;
  services.displayManager.autoLogin.user = username;

  # --- User
  users.users.${username} = {
    isNormalUser = true;
    createHome = true;
    extraGroups = [ "wheel" "dialout" ];
    initialPassword = "mct";
    shell = pkgs.bashInteractive;
  };
  security.sudo.wheelNeedsPassword = false;

  # Put Continue config into /etc/skel so it appears in the user's home.
  environment.etc."skel/.continue/config.yaml".text = continueConfigYaml;

  # Place VSIX directory (empty by default; user can populate in the repo before build).
  environment.etc."vscode-vsix/README.txt".text = ''
Drop VSIX files into ${vsixDir} inside the image.

Recommended filenames:
- Continue.continue.vsix
- TheLastOutpostWorkshop.arduino-maker-workshop.vsix

This repo contains scripts to download VSIX files and place them under assets/vsix/.
During the build, those VSIX files will be copied into ${vsixDir}.
'';

  # Copy VSIX assets from the repo into the image at build time.
  # If the directory is empty, the build still succeeds.
  system.activationScripts.copyVsix.text = ''
    mkdir -p ${vsixDir}
    if [ -d ${./../assets/vsix} ]; then
      cp -n ${./../assets/vsix}/*.vsix ${vsixDir}/ 2>/dev/null || true
    fi
  '';

  # Provide the installer script and a one-shot user service (disabled by default).
  environment.systemPackages = with pkgs; [
    google-chrome
    vscode-fhs

    # KDE apps
    kdePackages.konsole
    kdePackages.kate

    # Must-have tooling
    gitFull

    # CLI basics you asked for
    curl
    wget
    jq
    unzip
    zip
    tree
    ripgrep
    wl-clipboard
  ];

  # Bash: ls colors + LS_COLORS via dircolors
  programs.bash = {
    enableCompletion = true;
    interactiveShellInit = ''
      if command -v dircolors >/dev/null 2>&1; then
        eval "$(dircolors -b)"
      fi
      alias ls='ls --color=auto'
      alias ll='ls --color=auto -lah'
    '';
  };

  # A user-level systemd service to install VSIX extensions once.
  # Not enabled automatically to keep it "not intrusive".
  # You can enable it later by setting `systemd.user.services.mct-install-vscode-extensions.wantedBy`.
  systemd.user.services.mct-install-vscode-extensions = {
    description = "Install bundled VSIX extensions for VS Code (one-shot)";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${installExtensionsScript}";
    };
  };

  # Convenience command (manual run): `mct-install-vscode-extensions`
  environment.systemPackages = config.environment.systemPackages ++ [ installExtensionsScript ];

  # Locale defaults (Germany)
  i18n.defaultLocale = "de_DE.UTF-8";
  console.keyMap = "de";

  # Sensible base
  system.stateVersion = "25.11";
}
