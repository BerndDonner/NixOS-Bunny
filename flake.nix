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

      # Base VM list: bunny + bunny00..bunny11.
      # The same host data is used for normal and lockdown configurations.
      ids =
        [ "bunny" ]
        ++ (map (n: "bunny" + (lib.fixedWidthNumber 2 n)) (lib.range 0 11));

      hostFileFor = host:
        let p = ./hosts + ("/" + host + ".nix");
        in if builtins.pathExists p then p else ./hosts/default.nix;

      mkHost = { host, baseHost ? host, lockdown ? false }:
        let h = import (hostFileFor baseHost);
        in nixpkgs.lib.nixosSystem {
          inherit system;
          specialArgs = {
            inherit baseHost lockdown;
          };
          modules = [
            home-manager.nixosModules.home-manager
            ./modules/mct-vm.nix
          ]
          ++ lib.optionals lockdown [
            ./profiles/lockdown.nix
          ]
          ++ [
            # Host-specific settings (Nix-managed, reproducible)
            ({ ... }: {
              networking.hostName = host;

              home-manager.users.${username}.programs.git = {
                settings.user.name = h.gitName;
                settings.user.email = h.gitEmail;
              };
            })
          ];
        };

      normalConfs =
        builtins.listToAttrs (map (host: {
          name = host;
          value = mkHost { inherit host; };
        }) ids);

      lockdownConfs =
        builtins.listToAttrs (map (baseHost: {
          name = "${baseHost}-lockdown";
          value = mkHost {
            host = "${baseHost}-lockdown";
            inherit baseHost;
            lockdown = true;
          };
        }) ids);

      nixosConfs = normalConfs // lockdownConfs;

      bunnySystem = nixosConfs.bunny;
      bunnyLockdownSystem = nixosConfs."bunny-lockdown";

      packageHosts = ids ++ (map (host: "${host}-lockdown") ids);
    in {
      nixosConfigurations = nixosConfs;

      packages.${system} = let
        perHost =
          builtins.listToAttrs (builtins.concatLists (map (host: [
            {
              name = "${host}-qcow2";
              value = nixosConfs.${host}.config.system.build.images."qemu-efi";
            }
            {
              name = "${host}-vmware";
              value = nixosConfs.${host}.config.system.build.images.vmware;
            }
          ]) packageHosts));
      in
        perHost // {
          # Backwards-compatible shortcuts (golden image = bunny)
          qcow2  = bunnySystem.config.system.build.images."qemu-efi";
          vmware = bunnySystem.config.system.build.images.vmware;
          default = bunnySystem.config.system.build.images.vmware;

          # Lockdown golden image shortcuts
          qcow2-lockdown  = bunnyLockdownSystem.config.system.build.images."qemu-efi";
          vmware-lockdown = bunnyLockdownSystem.config.system.build.images.vmware;
        };
    };
}
