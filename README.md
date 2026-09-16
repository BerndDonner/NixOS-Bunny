# NixOS-Bunny — MCT VM configuration

NixOS configuration for the Microcontrollertechnik classroom VMs.

The repository builds the generic/golden Bunny image and the later
host-specific `bunny00`, `bunny01`, ... configurations. It is intentionally
separate from the `MCT_I3A` course repository: Bunny provides OS/user/Git
identity and recovery tooling; the course repository provides `upmaster`,
pre-commit policy and VS Code course protection.

## Git identity model

`rollout.csv` contains both human identity and the technical Forgejo login:

```text
name     -> Git user.name
email    -> Git user.email
forgejo  -> Git mct.student
```

For a student `huber` this later gives:

```text
user.name   = Anton Huber
user.email  = ...
mct.student = huber
```

`mct.student` is the common technical key used by MCT_I3A for:

```text
Forgejo login == student branch == student folder
```

The golden/teacher configuration `bunny` uses `mct.student = donner`.

Old `hosts/bunnyXX.nix` files from the previous school year do not contain a
`forgejo` field. They remain buildable with `mct.student = UNCONFIGURED`, but
MCT_I3A deliberately refuses student commits in that state. Before creating
new individualized student images, regenerate the host files from the current
`rollout.csv`.

## Safe Git defaults

The global Git configuration intentionally avoids implicit history rewriting:

```text
pull.rebase       = false
pull.ff           = only
rebase.autoStash  = false
merge.ff          = only
fetch.prune       = true
rerere.enabled    = true
```

Thus an accidental Pull may perform a harmless fast-forward, but it aborts on
divergence instead of creating a merge or starting a rebase.

Automatic repository maintenance is disabled:

```text
gc.auto           = 0
maintenance.auto  = false
```

This does **not** fix cloning directly onto a Windows/SMB network drive. The
known classroom workaround remains: clone on a local drive and then move the
complete repository to the network drive. Disabling automatic maintenance only
reduces later surprise repack operations.

## Recovery aliases

The global configuration keeps the teacher's fast recovery tools:

```text
git st
git lg
git br
git up
git current <path>
git incoming <path>
git undo
git discard
git reset-to-remote [branch] [--clean]
git abort-op
git doctor
```

`git doctor` is read-only and summarizes repository, branch, upstream,
ahead/behind, `mct.student`, current operation, status, remotes and recent
commits.

`git reset-to-remote` aborts an in-progress Git operation first and hard-resets
a non-master branch to its remote. Untracked files are preserved unless
`--clean` is explicitly supplied.

The MCT-specific `git upmaster` does **not** live here; it belongs to the course
repository.

## Build

Golden QEMU image:

```bash
nix build .#qcow2
```

Golden VMware image:

```bash
nix build .#vmware
```

The `bunny` host is the golden/teacher base. Student hosts are individualized
later by their `bunnyXX` configuration.

## Host generation from rollout.csv

Generate host files **before** individual student images are rebuilt:

```bash
./scripts/mct-vm.py generate-nix --csv rollout.csv --target-dir hosts
```

`generate-nix` now requires these fields for every VM row:

```text
vm, forgejo, name, email
```

It writes, for example:

```nix
{
  gitName  = "Anton Huber";
  gitEmail = "anton.huber@example.invalid";
  forgejo  = "huber";
}
```

## Current manual boundary

The golden image remains a deliberately reviewed artifact. After it exists,
the current workflow is:

```text
rollout.csv
  -> generate-nix (hosts/bunnyXX.nix)
  -> clone golden image to bunnyXX images
  -> boot each bunnyXX image
  -> sudo nixos-rebuild switch --flake .#bunnyXX
  -> prepare-images
  -> update-csv
  -> rollout
```

The repetitive boot/rebuild/shutdown part is intentionally left for the next
automation step; the data model is now ready for it.

## Main files

- `flake.nix` — all normal and lockdown NixOS configurations
- `modules/mct-vm.nix` — system/desktop/VM configuration
- `modules/home/student.nix` — Home Manager entry point for `student`
- `modules/home/modules/git.nix` — global Git defaults and recovery aliases
- `hosts/*.nix` — host-specific Git identity
- `scripts/mct_vm/nixgen.py` — generates host identities from rollout CSV
- `scripts/mct-vm.py` — classroom image/rollout helper
- `scripts/mct-vm-lockdown.py` — lockdown image/rollout helper
