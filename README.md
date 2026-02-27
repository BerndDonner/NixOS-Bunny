# Tabby-Replacement (MCT) — NixOS 25.11 VM image config

This repo builds a **NixOS 25.11** VM image for **Microcontrollertechnik** with:

- KDE Plasma (Wayland default, X11 selectable)
- VMware guest integration
- Google Chrome (unfree)
- VS Code **vscode-fhs**
- Continue config at `~/.continue/config.yaml` (user scope via `/etc/skel`)
- Git + your aliases (no `user.name` / `user.email` on purpose)
- Tools: `curl`, `wget`, `jq`, `unzip`, `zip`, `tree`, `rg`, `wl-copy`
- Bash: `ls --color=auto` + `LS_COLORS` via `dircolors`

> Seed/injection is intentionally **not** included yet (per your decision). This is iteration-friendly.

## Build (VMware)

```bash
nix build .#vmware
```

Output is in `./result/`.

Optional:

```bash
nix build .#qcow2
nix build .#virtualbox
```

## VS Code Extensions (offline via VSIX)

This config supports bundling VSIX files into the image at `/opt/vscode-vsix`.

1. Download VSIX files into `assets/vsix/`:

```bash
./scripts/fetch-vsix.sh <continue_version> <arduino_maker_workshop_version>
```

2. Build the image.

3. Inside the VM, install the bundled extensions once:

```bash
mct-install-vscode-extensions
```

This keeps things "not intrusive" (no autoinstall), but still fully offline-capable.

If you later want a one-shot autoinstall on first login, we can simply add a `WantedBy=default.target` to the provided user service.

## Default user

- username: `student`
- password: `mct`
- groups: `wheel`, `dialout`
- sudo: no password for wheel

## Files

- `modules/mct-vm.nix` — main system config
- `modules/git-module.nix` — git config (without identity)
- `assets/vsix/` — put VSIX files here before building
- `scripts/fetch-vsix.sh` — helper to download VSIX
