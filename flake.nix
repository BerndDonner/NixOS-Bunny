{
  description = "Microcontrollertechnik - NixOS 26.05 VM image config";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    home-manager.url = "github:nix-community/home-manager/release-26.05";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";

    disko.url = "github:nix-community/disko/latest";
    disko.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, home-manager, disko }:
    let
      x86System = "x86_64-linux";
      armSystem = "aarch64-linux";
      lib = nixpkgs.lib;

      username = "student";

      toolConfig = builtins.fromTOML (builtins.readFile ./scripts/config/config.toml);
      forgejoHost = toolConfig.forgejo.host;
      targetMode = toolConfig.workflow.mode;
      targetArch = toolConfig.workflow.arch;
      targetSystem =
        if targetMode == "lockdown" && targetArch == "arm64" then
          throw "Lockdown/exam images are amd64-only"
        else if targetArch == "amd64" then x86System
        else if targetArch == "arm64" then armSystem
        else throw "Unsupported [workflow].arch in scripts/config/config.toml: ${targetArch}";

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
          system = targetSystem;
          specialArgs = {
            inherit baseHost lockdown forgejoHost;
          } // lib.optionalAttrs (targetSystem == armSystem) {
            # Disko builds the ARM image from the current x86_64 build host and
            # uses binfmt for target-side aarch64 install steps.
            armImageBuilderPkgs = nixpkgs.legacyPackages.${x86System};
          };
          modules = [
            home-manager.nixosModules.home-manager
            ./modules/mct-vm.nix
          ]
          ++ lib.optionals (targetSystem == armSystem) [
            disko.nixosModules.disko
            ./modules/arm-image.nix
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
                # in the course hooks until generate-hosts is run.
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

      # Lockdown is exam infrastructure and deliberately exists only for amd64.
      lockdownConfs =
        if targetArch == "amd64" then
          builtins.listToAttrs (map (baseHost: {
            name = "${baseHost}-lockdown";
            value = mkHost {
              host = "${baseHost}-lockdown";
              inherit baseHost;
              lockdown = true;
            };
          }) ids)
        else
          {};

      nixosConfs = normalConfs // lockdownConfs;

      imageFor = host:
        if targetArch == "amd64" then
          nixosConfs.${host}.config.system.build.images."qemu-efi"
        else
          nixosConfs.${host}.config.system.build.diskoImages;

      packageHosts =
        ids
        ++ lib.optionals (targetArch == "amd64") (map (host: "${host}-lockdown") ids);

      targetPackages =
        let
          perHost = builtins.listToAttrs (map (host: {
            name = "${host}-qcow2";
            value = imageFor host;
          }) packageHosts);
        in
          perHost
          // {
            # Golden image shortcut. QCOW2 is the canonical build artifact;
            # deployment formats such as VMDK are exported only after build-vms.
            qcow2 = imageFor "bunny";
            default = imageFor "bunny";
          }
          // lib.optionalAttrs (targetArch == "amd64") {
            qcow2-lockdown = imageFor "bunny-lockdown";
          };
    in {
      nixosConfigurations = nixosConfs;
      packages.${targetSystem} = targetPackages;
    };
}
