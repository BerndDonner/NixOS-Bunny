{
  description = "Microcontrollertechnik - NixOS 25.11 VMware image config";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    nixos-generators.url = "github:nix-community/nixos-generators";
    nixos-generators.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nixos-generators }:
    let
      system = "x86_64-linux";
      mk = format: nixos-generators.nixosGenerate {
        inherit system format;
        modules = [ ./modules/mct-vm.nix ];
      };
    in {
      packages.${system} = {
        vmware = mk "vmware";
        qcow2  = mk "qcow2";   # optional, kann auch komplett raus
        default = mk "vmware"; # optional: nix build ohne .#...
      };
    };
}
