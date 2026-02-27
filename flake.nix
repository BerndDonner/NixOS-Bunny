{
  description = "Tabby-Replacement (MCT) - NixOS 25.11 VMware image config";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    nixos-generators.url = "github:nix-community/nixos-generators";
    nixos-generators.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nixos-generators }:
    let
      system = "x86_64-linux";
    in {
      packages.${system}.vmware = nixos-generators.nixosGenerate {
        inherit system;
        format = "vmware";
        modules = [ ./modules/mct-vm.nix ];
      };

      # Optional formats
      packages.${system}.qcow2 = nixos-generators.nixosGenerate {
        inherit system;
        format = "qcow2";
        modules = [ ./modules/mct-vm.nix ];
      };

      packages.${system}.virtualbox = nixos-generators.nixosGenerate {
        inherit system;
        format = "virtualbox";
        modules = [ ./modules/mct-vm.nix ];
      };
    };
}
