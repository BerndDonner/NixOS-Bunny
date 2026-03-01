{
  description = "Microcontrollertechnik - NixOS 25.11 VMware image config";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    nixos-generators.url = "github:nix-community/nixos-generators";
    nixos-generators.inputs.nixpkgs.follows = "nixpkgs";

    home-manager.url = "github:nix-community/home-manager/release-25.11";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nixos-generators, home-manager }:
    let
      system = "x86_64-linux";
      mk = format: nixos-generators.nixosGenerate {
        inherit system format;
        modules = [
          home-manager.nixosModules.home-manager
          ./modules/mct-vm.nix
        ];
      };
    in {
      packages.${system} = {
        vmware = mk "vmware";
        qcow2  = mk "qcow-efi";   # optional, kann auch komplett raus
        default = mk "vmware"; # optional: nix build ohne .#...
      };

      # Convenience aliases (top-level)
      # vmware = self.packages.${system}.vmware;
      # qcow2  = self.packages.${system}.qcow2;
    };
}
