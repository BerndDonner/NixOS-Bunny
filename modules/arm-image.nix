{ armImageBuilderPkgs, ... }:

{
  # ARM-only image builder for Apple-Silicon/VMware-Fusion testing.
  #
  # Disko runs an x86_64 builder VM and uses binfmt for the aarch64 target-side
  # install steps. This avoids the legacy make-disk-image/cptofs path whose LKL
  # helper is hard-limited to 100 MiB.
  disko = {
    # mct-vm.nix already defines the runtime filesystems by their filesystem
    # labels. Disko is used here only to create/populate the ARM image.
    enableConfig = false;

    # Memory of Disko's native x86_64 builder VM, not the final Bunny VM.
    memSize = 4096;

    imageBuilder = {
      enableBinfmt = true;
      pkgs = armImageBuilderPkgs;
      kernelPackages = armImageBuilderPkgs.linuxPackages_latest;
      imageFormat = "qcow2";
      name = "bunny-arm-disko-image";
    };

    devices.disk.main = {
      type = "disk";
      device = "/dev/vda";
      imageName = "bunny-arm";

      # QCOW2 is sparse, so this is logical capacity rather than immediate host
      # disk consumption. Leave enough room for the desktop/VS-Code closure and
      # later manual golden-image work.
      imageSize = "32G";

      content = {
        type = "gpt";
        partitions = {
          ESP = {
            type = "EF00";
            size = "512M";
            content = {
              type = "filesystem";
              format = "vfat";
              mountpoint = "/boot";
              mountOptions = [ "umask=0077" ];

              # Keep the label expected by modules/mct-vm.nix.
              extraArgs = [ "-n" "ESP" ];
            };
          };

          root = {
            size = "100%";
            content = {
              type = "filesystem";
              format = "ext4";
              mountpoint = "/";

              # Keep the label expected by modules/mct-vm.nix.
              extraArgs = [ "-L" "nixos" ];
            };
          };
        };
      };
    };
  };

  # TODO(repart): Prefer NixOS' native systemd-repart image builder once
  # https://github.com/NixOS/nixpkgs/issues/508743 is fixed in the nixpkgs
  # revision used by NixOS-Bunny and Home Manager activation has been verified
  # on such an image. Until then keep Disko for the ARM image path so the Nix
  # store database is populated correctly.
}
