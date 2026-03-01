{
  description = "Microcontrollertechnik - NixOS 25.11 VM image config";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    home-manager.url = "github:nix-community/home-manager/release-25.11";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, home-manager }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;

      username = "student";

      # Create host list: bunny + bunny00..bunny11
      ids =
        [ "bunny" ]
        ++ (map (n: "bunny" + (lib.fixedWidthNumber 2 n)) (lib.range 0 11));

      hostFileFor = host:
        let p = ./hosts + ("/" + host + ".nix");
        in if builtins.pathExists p then p else ./hosts/default.nix;

      mkHost = host:
        let h = import (hostFileFor host);
        in nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            home-manager.nixosModules.home-manager
            ./modules/mct-vm.nix

            # Host-specific settings (Nix-managed, reproducible)
            ({ ... }: {
              networking.hostName = host;

              home-manager.users.${username}.programs.git = {
                userName  = h.gitName;
                userEmail = h.gitEmail;
              };
            })
          ];
        };

      nixosConfs =
        builtins.listToAttrs (map (host: { name = host; value = mkHost host; }) ids);

      bunnySystem = nixosConfs.bunny;
    in {
      nixosConfigurations = nixosConfs;

      packages.${system} = {
        # nixpkgs images (upstreamed nixos-generators functionality)
        qcow2  = bunnySystem.config.system.build.images."qemu-efi";
        vmware = bunnySystem.config.system.build.images.vmware;
        default = bunnySystem.config.system.build.images.vmware;
      };
    };
}
