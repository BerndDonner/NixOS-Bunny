{
  description = "Microcontrollertechnik - NixOS 26.05 VM image config";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    home-manager.url = "github:nix-community/home-manager/release-26.05";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, home-manager }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;

      username = "student";

      # The host files are generated from scripts/config/rollout.csv. Discover exactly the
      # active bunnyXX definitions instead of maintaining a second VM list here.
      # bunny.nix remains the generic/golden configuration.
      activeHostFiles =
        lib.filter
          (name: (builtins.match "bunny[0-9][0-9]\\.nix" name) != null)
          (builtins.attrNames (builtins.readDir ./hosts));

      ids =
        [ "bunny" ]
        ++ (map (name: lib.removeSuffix ".nix" name) activeHostFiles);

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
                # forgejo is the technical course identity used by every MCT course repository:
                # mct.student == Forgejo login == student branch == student folder.
                # Old host files without forgejo stay buildable but fail closed
                # in the course hooks until regenerate-nix is run.
                settings.mct.student = if h ? forgejo then h.forgejo else "UNCONFIGURED";
                settings.mct.course = if h ? course then h.course else "UNCONFIGURED";
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
          builtins.listToAttrs (map (host: {
            name = "${host}-qcow2";
            value = nixosConfs.${host}.config.system.build.images."qemu-efi";
          }) packageHosts);
      in
        perHost // {
          # Golden image shortcut (bunny). QCOW2 is the canonical build artifact;
          # deployment formats such as VMDK are exported only after phase 3.
          qcow2 = bunnySystem.config.system.build.images."qemu-efi";
          default = bunnySystem.config.system.build.images."qemu-efi";

          # Lockdown golden image shortcut
          qcow2-lockdown = bunnyLockdownSystem.config.system.build.images."qemu-efi";
        };
    };
}
