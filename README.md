# NixOS-Bunny — MCT VM configuration

NixOS configuration for the Microcontrollertechnik classroom VMs.

The repository builds the generic/golden Bunny image and the later
host-specific `bunny00`, `bunny01`, ... configurations. It is intentionally
separate from the generated course repositories (`MCT_I3A`, `MCT_E3A`, ...):
Bunny provides OS/user/Git identity and recovery tooling; the course repository
provides `upmaster`,
pre-commit policy and VS Code course protection.

## Git identity model

`rollout.csv` contains the human identity, technical Forgejo login and course:

```text
full_name -> Git user.name
email     -> Git user.email
forgejo   -> Git mct.student
course    -> Git mct.course
```

For a student `huber` this later gives:

```text
user.name   = Anton Huber
user.email  = ...
mct.student = huber
```

`mct.student` is the common technical key used by every generated MCT course
repository for:

```text
Forgejo login == student branch == student folder
```

`mct.course` records which class repository belongs to the VM (`I3A` or `E3A`).
The generic golden host `bunny` deliberately uses `mct.course = UNCONFIGURED`.

The golden/teacher configuration `bunny` uses `mct.student = donner`.

`hosts/bunnyXX.nix` is generated data.  Before building the golden image,
regenerate it from the current `rollout.csv`.  The generator removes stale
`bunnyXX.nix` files automatically, and `flake.nix` discovers the remaining host
files dynamically.  There is therefore only one active-VM list to maintain: the
CSV.

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

## Provisioning SSH

Bunny enables OpenSSH for later VM provisioning, but uses systemd socket
activation (`services.openssh.startWhenNeeded = true`). Normally only
`sshd.socket` listens on port 22; the `sshd` process is started on demand for a
connection. NixOS tests this socket-activated mode directly.

Access is deliberately narrow:

```text
user                 student
password login       disabled
keyboard-interactive disabled
root login           disabled
authorized key       ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFOwgNuwt6tb2+fz7KQ6g+rH5gBCS58d6d7Y1A2O5bMX bernd@tracy
```

This key is the **public** provisioning key only. No private key or passphrase
is stored in Bunny. Because `student` is already in `wheel` with passwordless
`sudo`, later provisioning can perform the required system and user setup over
this SSH connection.

The socket can be inspected with:

```bash
systemctl status sshd.socket
```

and the actual daemon will normally exist only while a connection is active.

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

`generate-nix` uses active VM rows and requires:

```text
vm, course, forgejo, full_name, email
```

It also removes stale `hosts/bunnyXX.nix` files that no longer occur as active
rows. It writes, for example:

```nix
{
  gitName  = "Thomas Pabst";
  gitEmail = "thomas.pabst@sabel.education";
  forgejo  = "pabst";
  course   = "I3A";
}
```

## First-boot bootstrap of NixOS-Bunny

The public repository is cloned automatically on first boot:

```text
https://github.com/BerndDonner/NixOS-Bunny.git
    -> /home/student/NixOS-Bunny
```

After a successful clone, `/etc/nixos` is replaced by a symlink to that working
copy, so plain `sudo nixos-rebuild switch` uses the checked-out flake and the
configuration matching the current hostname.  An existing clone is never
automatically pulled or modified; the golden image remains a reviewed snapshot.

The service can be inspected or retried with:

```bash
systemctl status mct-bootstrap-nixos-bunny.service
sudo systemctl restart mct-bootstrap-nixos-bunny.service
```

## Copying the Arduino offline documentation tree

The offline documentation is intentionally not a Nix input.  Its source root can
be copied once into the golden VM over the already configured provisioning SSH:

```bash
./scripts/copy-home-tree.sh /path/to/source-root <VM-address>
```

The *contents* of that source root are extracted directly into `/home/student`,
including hidden files and symlinks.  This is useful when the supplied directory
tree already has exactly the layout that should appear below the student's home.

VS Code extensions remain a deliberate one-time manual golden-image step.

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
