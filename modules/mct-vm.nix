{ config, pkgs, lib, ... }:

let
  username = "student";
in {
  imports = [ ];

  nixpkgs.config.allowUnfree = true;

  # Flakes-only setup (keine Channels)

  # Make nixos-rebuild (flake-mode) work without --flake:
  # nixos-rebuild looks for /etc/nixos/flake.nix and uses nixosConfigurations.<hostname>.
  system.activationScripts.mctLinkEtcNixos.text = ''
    set -e
    target="/home/student/NixOS-Bunny"
    if [ -e "$target/flake.nix" ]; then
      if [ -L /etc/nixos ]; then
        # ok
        :
      else
        rm -rf /etc/nixos
        ln -s "$target" /etc/nixos
      fi
    fi
  '';

  nix = {
    settings.experimental-features = [ "nix-command" "flakes" ];
    nixPath = lib.mkForce [ ];
  };

  # Home Manager (flake-based) for the default user
  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    users.${username} = import ./home/student.nix;
  };
  time.timeZone = "Europe/Berlin";

  # VM guest integration
  virtualisation.vmware.guest.enable = true;


  # --- Boot + filesystems (required for nixos-rebuild switch on a running VM)

  boot.loader.systemd-boot.enable = true;

  # In VMs i.d.R. kein echtes EFI-NVRAM persistent → nicht versuchen, Variablen zu schreiben
  boot.loader.efi.canTouchEfiVariables = false;
  boot.loader.efi.efiSysMountPoint = "/boot";

  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  fileSystems."/boot" = {
    device = "/dev/disk/by-label/ESP";
    fsType = "vfat";
  };

  # --- Initrd: storage/network drivers for portable VM images (QEMU + VMware)
  # Some image builders don't auto-include the right modules. Ensure root disk appears early.
  boot.initrd.availableKernelModules = lib.mkBefore [
    # QEMU virtio
    "virtio" "virtio_pci" "virtio_ring"
    "virtio_blk" "virtio_scsi"
    # SATA/AHCI/ATA fallback
    "ahci" "ata_piix"
    # Generic SCSI / disk
    "sd_mod" "sr_mod" "scsi_mod"
    # NVMe (harmless, useful on some setups)
    "nvme"
    # VMware storage (if image later runs there)
    "vmw_pvscsi"
  ];

  # Load the important ones early (keeps /dev/disk/by-label/* working reliably)
  boot.initrd.kernelModules = lib.mkBefore [
    "virtio_pci" "virtio_blk" "virtio_scsi"
    "ahci" "ata_piix"
    "vmw_pvscsi"
  ];

  # --- Desktop: KDE Plasma (Wayland default; X11 selectable)
  services.xserver.enable = true;
  services.xserver.videoDrivers = [ "vmware" ];

  services.xserver.xkb = {
    layout = "de";
    variant = "nodeadkeys";
  };
  

  services.displayManager.sddm.enable = true;
  services.displayManager.sddm.wayland.enable = true;
  services.displayManager.defaultSession = "plasma";

  services.desktopManager.plasma6.enable = true;

  # KWallet (Plasma 6) via PAM, so the wallet unlocks on login
  security.pam.services.sddm.kwallet.enable = true;

  # Autologin
  services.displayManager.autoLogin.enable = false;
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

  # Nerd Fonts for Konsole/VS Code/Terminals (and icons in prompts)
  fonts = {
    packages = with pkgs; [
      nerd-fonts.jetbrains-mono
      nerd-fonts.fira-code
      nerd-fonts.ubuntu-mono
    ];

    fontconfig.defaultFonts = {
      monospace = [ "JetBrainsMono Nerd Font" "FiraCode Nerd Font" "UbuntuMono Nerd Font" ];
      sansSerif = [ "Noto Sans" ];
      serif = [ "Noto Serif" ];
    };
  };

  # Provide the installer script and a one-shot user service (disabled by default).
  environment.systemPackages = with pkgs; [
    google-chrome
    vscode-fhs

    # KDE apps
    kdePackages.konsole
    kdePackages.kate
    kdePackages.kwallet-pam

    # Must-have tooling
    gitFull

    # Editor
    micro

    # CLI basics you asked for
    curl
    wget
    jq
    unzip
    zip
    tree
    ripgrep
    wl-clipboard
    magic-wormhole

  ];

  # Bash: ls colors + LS_COLORS via dircolors
  programs.bash = {
    completion.enable = true;
    interactiveShellInit = ''
      if command -v dircolors >/dev/null 2>&1; then
        eval "$(dircolors -b)"
      fi
      alias ls='ls --color=auto'
      alias ll='ls --color=auto -lah'
    '';
  };

  # Locale defaults (Germany)
  i18n.defaultLocale = "de_DE.UTF-8";
  console.useXkbConfig = true;
  
  # Sensible base
  system.stateVersion = "25.11";
}
