# Bunny (MCT) — NixOS 25.11 VM image config

This repo builds a **NixOS 25.11** VM image for **Microcontrollertechnik** with:

- KDE Plasma (Wayland default, X11 selectable)
- VMware guest integration
- Google Chrome (unfree)
- VS Code **vscode-fhs**
- Nerd Fonts (JetBrainsMono/FiraCode/UbuntuMono Nerd Font)
- Home Manager (flakes-only) for user-scoped config
- Git + your aliases (no `user.name` / `user.email` on purpose; lives in Home Manager)
- micro editor with sensible defaults
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

## Default user

- username: `student`
- password: `mct`
- groups: `wheel`, `dialout`
- sudo: no password for wheel

## Files

- `modules/mct-vm.nix` — main system config
- `modules/home/student.nix` — Home Manager config for `student` (git + micro + defaults)
