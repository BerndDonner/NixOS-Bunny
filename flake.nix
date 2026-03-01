{
  description = "Microcontrollertechnik - NixOS 25.11 VMware image config";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    home-manager.url = "github:nix-community/home-manager/release-25.11";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, home-manager }:
    let
      system = "x86_64-linux";

      bunnySystem = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          home-manager.nixosModules.home-manager
          ./modules/mct-vm.nix
        ];
      };
    in {
      nixosConfigurations.bunny = bunnySystem;

      packages.${system} = {
        # neue upstreamed image builder: system.build.images.<variant>
        # Variantenamen sind wie bei nixos-generators (z.B. vmware, qcow-efi, raw-efi, ...)
        vmware = bunnySystem.config.system.build.images.vmware;
        qcow2  = bunnySystem.config.system.build.images."qemu-efi";

        default = bunnySystem.config.system.build.images.vmware;
      };
    };
}
