#!/usr/bin/env python3
"""Deterministic edit script for the 'bunny' golden-image config.

Run from the repo root:
  python3 scripts/apply_bunny_changes.py

It will:
- Add home-manager input + module wiring to flake.nix
- Convert git config to Home Manager (creates modules/home/student.nix)
- Remove Continue + VSIX plumbing from modules/mct-vm.nix
- Install Nerd Fonts + micro + kwallet-pam
- Set hostname to 'bunny'
- Update README.md accordingly
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def _replace_once(path: Path, pattern: str, repl: str) -> None:
    s = _read(path)
    ns, n = re.subn(pattern, repl, s, count=1, flags=re.DOTALL)
    if n != 1:
        raise SystemExit(f"Patch failed for {path}: expected 1 match, got {n}")
    _write(path, ns)


def main() -> None:
    flake = ROOT / "flake.nix"
    mct = ROOT / "modules" / "mct-vm.nix"
    readme = ROOT / "README.md"

    # --- flake.nix: add home-manager input + outputs wiring
    s = _read(flake)
    if "home-manager.url" not in s:
        s = s.replace(
            "nixos-generators.inputs.nixpkgs.follows = \"nixpkgs\";\n",
            "nixos-generators.inputs.nixpkgs.follows = \"nixpkgs\";\n\n"
            "    home-manager.url = \"github:nix-community/home-manager/release-25.11\";\n"
            "    home-manager.inputs.nixpkgs.follows = \"nixpkgs\";\n",
        )
    s = re.sub(
        r"outputs\s*=\s*\{\s*self,\s*nixpkgs,\s*nixos-generators\s*\}:",
        "outputs = { self, nixpkgs, nixos-generators, home-manager }:",
        s,
        count=1,
    )
    s = re.sub(
        r"modules\s*=\s*\[\s*\./modules/mct-vm\.nix\s*\];",
        "modules = [\n          home-manager.nixosModules.home-manager\n          ./modules/mct-vm.nix\n        ];",
        s,
        count=1,
    )
    _write(flake, s)

    # --- modules/mct-vm.nix: remove Continue/VSIX blocks (broad delete ranges)
    ms = _read(mct)

    # remove the big let-bindings for continue/vsix
    ms = re.sub(
        r"\n\s*continueConfigYaml\s*=\s*''.*?'';\n",
        "\n",
        ms,
        flags=re.DOTALL,
    )
    ms = re.sub(
        r"\n\s*# Where VSIX files will live.*?installExtensionsScript\s*=\s*pkgs\.writeShellScriptBin.*?'';\n",
        "\n",
        ms,
        flags=re.DOTALL,
    )

    # drop import of git-module.nix
    ms = re.sub(r"imports\s*=\s*\[\s*\./git-module\.nix\s*\];", "imports = [ ];", ms)

    # host name
    ms = re.sub(r"networking\.hostName\s*=\s*\"[^\"]+\";", 'networking.hostName = "bunny";', ms)

    # add flakes-only nix settings if missing
    if "experimental-features" not in ms:
        ms = ms.replace(
            "nixpkgs.config.allowUnfree = true;",
            "nixpkgs.config.allowUnfree = true;\n\n"
            "  # Flakes-only setup (keine Channels)\n"
            "  nix = {\n"
            "    settings.experimental-features = [ \"nix-command\" \"flakes\" ];\n"
            "    nixPath = lib.mkForce [ ];\n"
            "  };\n\n"
            "  # Home Manager (flake-based) for the default user\n"
            "  home-manager = {\n"
            "    useGlobalPkgs = true;\n"
            "    useUserPackages = true;\n"
            "    users.${username} = import ./home/student.nix;\n"
            "  };",
        )

    # remove Continue + VSIX etc/skel, etc, activation scripts, user service
    ms = re.sub(r"\n\s*# Put Continue config.*?\n\s*security\.sudo\.wheelNeedsPassword\s*=\s*false;\n", "\n  security.sudo.wheelNeedsPassword = false;\n", ms, flags=re.DOTALL)
    ms = re.sub(r"\n\s*# Place VSIX directory.*?\n\s*system\.activationScripts\.copyVsix\.text\s*=\s*''.*?'';\n", "\n", ms, flags=re.DOTALL)
    ms = re.sub(r"\n\s*systemd\.user\.services\.mct-install-vscode-extensions\s*=\s*\{.*?\};\n", "\n", ms, flags=re.DOTALL)

    # add kwallet pam (common option name)
    if "enableKwallet" not in ms:
        ms = ms.replace(
            "services.desktopManager.plasma6.enable = true;",
            "services.desktopManager.plasma6.enable = true;\n\n  # KWallet via PAM, so the wallet unlocks on login\n  security.pam.services.sddm.enableKwallet = true;",
        )

    # add fonts block if missing
    if "fonts =" not in ms:
        ms = ms.replace(
            "security.sudo.wheelNeedsPassword = false;",
            "security.sudo.wheelNeedsPassword = false;\n\n"
            "  # Nerd Fonts for Konsole/VS Code/Terminals (and icons in prompts)\n"
            "  fonts = {\n"
            "    packages = with pkgs; [\n"
            "      nerd-fonts.jetbrains-mono\n"
            "      nerd-fonts.fira-code\n"
            "      nerd-fonts.ubuntu-mono\n"
            "    ];\n\n"
            "    fontconfig.defaultFonts = {\n"
            "      monospace = [ \"JetBrainsMono Nerd Font\" \"FiraCode Nerd Font\" \"UbuntuMono Nerd Font\" ];\n"
            "      sansSerif = [ \"Noto Sans\" ];\n"
            "      serif = [ \"Noto Serif\" ];\n"
            "    };\n"
            "  };",
        )

    # add micro + kwallet-pam packages
    ms = ms.replace(
        "kdePackages.kate\n",
        "kdePackages.kate\n    kdePackages.kwallet-pam\n",
    )
    if "micro" not in ms:
        ms = ms.replace(
            "# Must-have tooling\n    gitFull\n",
            "# Must-have tooling\n    gitFull\n\n    # Editor\n    micro\n",
        )

    # drop installExtensionsScript from systemPackages if still present
    ms = re.sub(r"\n\s*installExtensionsScript\n", "\n", ms)

    _write(mct, ms)

    # --- Remove old files if present
    for p in [ROOT / "modules" / "git-module.nix", ROOT / "scripts" / "fetch-vsix.sh"]:
        if p.exists():
            p.unlink()

    vsix_dir = ROOT / "assets" / "vsix"
    if vsix_dir.exists():
        # remove whole dir
        for child in sorted(vsix_dir.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()

    # --- Create Home Manager user config
    student_nix = ROOT / "modules" / "home" / "student.nix"
    if not student_nix.exists():
        student_nix.write_text(
            """{ config, pkgs, ... }:\n\n{\n  home.username = \"student\";\n  home.homeDirectory = \"/home/student\";\n  home.stateVersion = \"25.11\";\n\n  home.sessionVariables = {\n    EDITOR = \"micro\";\n    VISUAL = \"micro\";\n  };\n\n  programs.git = {\n    enable = true;\n    package = pkgs.gitFull;\n    extraConfig = {\n      init.defaultBranch = \"master\";\n      pull.rebase = true;\n      rebase.autoStash = true;\n      fetch.prune = true;\n      rerere.enabled = true;\n      merge.ff = \"only\";\n    };\n  };\n\n  home.file.\".config/micro/settings.json\".text = builtins.toJSON {\n    autosu = true;\n    colorscheme = \"default\";\n    mouse = true;\n    tabsize = 2;\n    tabstospaces = true;\n    softwrap = true;\n    ruler = true;\n    statusline = true;\n  };\n}\n""",
            encoding="utf-8",
        )

    # --- README refresh: minimal, safe substitutions
    rs = _read(readme)
    rs = rs.replace("Tabby-Replacement (MCT)", "Bunny (MCT)")
    rs = re.sub(r"\n## VS Code Extensions.*?(?=\n## Default user)", "\n", rs, flags=re.DOTALL)
    rs = rs.replace("- `modules/git-module.nix` — git config (without identity)\n", "")
    rs = rs.replace("- `assets/vsix/` — put VSIX files here before building\n", "")
    rs = rs.replace("- `scripts/fetch-vsix.sh` — helper to download VSIX\n", "")
    if "modules/home/student.nix" not in rs:
        rs = rs.replace(
            "- `modules/mct-vm.nix` — main system config\n",
            "- `modules/mct-vm.nix` — main system config\n- `modules/home/student.nix` — Home Manager config for `student` (git + micro + defaults)\n",
        )
    _write(readme, rs)

    print("OK: bunny changes applied.\n\nNext step:\n  nix flake lock --update-input home-manager")


if __name__ == "__main__":
    main()
