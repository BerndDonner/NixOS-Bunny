{
  description = "NixOS-Bunny management tools";

  inputs = {
    nixos-config.url = "github:BerndDonner/NixOS-Config";
    nixpkgs.follows = "nixos-config/nixpkgs";
  };

  outputs = { self, nixpkgs, nixos-config, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
      };

      pythonDev = nixos-config.lib.mkPythonDevShell;
    in
    {
      devShells.${system}.default = pythonDev {
        inherit pkgs;
        inputs = { inherit nixos-config; };
        flakeLockPath = ./flake.lock;
        symbol = "🐍";
        pythonVersion = pkgs.python3;

        # qemu/qemu-img intentionally come from the host so the management
        # shell does not shadow a deliberately selected host QEMU version.
        extraPackages = with pkgs; [
          curl
          git
          openssh
          zstd
        ];

        message = "🐍 NixOS-Bunny management shell ready";
      };
    };
}
