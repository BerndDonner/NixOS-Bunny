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
authorized key       assets/ssh/mct-vm-setup.pub
```

The public setup key is versioned as `assets/ssh/mct-vm-setup.pub` and Nix
builds exactly that key into Bunny. The private half is never stored in the
repository; its preparation-host path comes from
`[provisioning].preparation_host_key` in `config.toml` (normally
`~/.ssh/mct-vm-setup`). An ssh-agent is not required. `config-check` derives the
public key from the configured private key and verifies that it matches the
versioned key. mct-vm never creates or rotates setup keys implicitly. Because
`student` is already in `wheel` with passwordless `sudo`, later provisioning can
perform the required system and user setup over this SSH connection.

The socket can be inspected with:

```bash
systemctl status sshd.socket
```

and the actual daemon will normally exist only while a connection is active.

## Configuration and image lifecycle

`mct-vm.py` has deliberately no command-line options. All persistent settings,
temporary selections and operational documentation live in the repository-root
`config.toml`. The command line only chooses the operation:

```bash
./scripts/mct-vm.py config-check
./scripts/mct-vm.py generate-nix
./scripts/mct-vm.py prepare-golden
./scripts/mct-vm.py finalize-golden
./scripts/mct-vm.py clone
./scripts/mct-vm.py individualize
./scripts/mct-vm.py prepare-images
./scripts/mct-vm.py update-csv
./scripts/mct-vm.py rollout
```

`[workflow].mode` selects exactly one image family: `classroom` or `lockdown`.
They are alternatives, not parallel profiles. Classroom mode uses `rollout.csv`
and `bunnyXX.*`; lockdown mode uses `rollout-lockdown.csv` and
`bunnyXX-lockdown.*`. Lockdown individualization is intentionally not expanded
further until that exam workflow is reviewed again.

Temporary one-run controls are grouped visibly under `[run]` in `config.toml`.
Non-default temporary values are printed before an operation starts.

**Phases 1, 2 and 3 use QCOW2 only.** VMDK conversion happens only after the
host-specific images are finished.

### Phase 1 — build the generic QCOW2

```bash
nix build .#qcow2
```

Name/copy the image as the active `[golden_image].file` from `config.toml`.
The UEFI state filename is derived automatically by replacing `.qcow2` with
`.OVMF_VARS.fd`.

### Phase 2a — prepare the golden image

```bash
./scripts/mct-vm.py prepare-golden
```

This starts the configured golden image **visibly**, with the fixed provisioning
SSH transport `student@127.0.0.1:2222`. The transport itself is fixed, while
the preparation-host private key is selected by
`[provisioning].preparation_host_key`. Its public half is
`assets/ssh/mct-vm-setup.pub` and is built into Bunny by Nix.

`prepare-golden` performs the work that is safe before manual GUI setup:

- optionally overlays `[golden_image].student_home_content` onto
  `/home/student`;
- includes hidden regular files, preserves unrelated guest files, and ignores
  symlinks/empty directories;
- **never** copies `.continue/config.yaml`;
- deliberately does **not** set the final Chrome start page yet;
- leaves the VM running for manual work.

The old `scripts/copy-home-tree.sh` has been absorbed into this command.

Now perform the deliberate manual golden-image work, especially installing and
starting the VS Code extensions/Continue so they can create whatever initial
state they need. Chrome may also be used freely during this phase. Before
finalization, remove the Chrome profile/state you do not want in the golden image
(e.g. passwords, cookies, logins and history); the final offline start page is
installed only afterwards by `finalize-golden`.

### Phase 2b — finalize the golden image

```bash
./scripts/mct-vm.py finalize-golden
```

If the VM from `prepare-golden` is still running, the command reconnects to that
exact session. If it was shut down meanwhile, it starts the configured golden
image again headless.

Finalization:

1. installs the authoritative `assets/continue/config.yaml` as
   `~/.continue/config.yaml` **after** Continue has been installed/started;
2. verifies the copied Continue configuration;
3. verifies `[golden_image].browser_start_page` and installs the final managed
   Chrome policy **after** all manual Chrome use/cleanup;
4. optionally optimizes image size (`[images].optimize_image_size`; currently
   implemented with guest `fstrim` plus QEMU discard);
5. shuts the VM down cleanly.

### Host generation

`hosts/bunnyXX.nix` is generated from the **active mode's** rollout CSV:

```bash
./scripts/mct-vm.py generate-nix
```

The host files are reviewed configuration. `individualize` never regenerates
them implicitly.

### Clone host-specific images

```bash
./scripts/mct-vm.py clone
```

`clone` copies the configured golden QCOW2 and its matching UEFI state to the
active VMs. It is intentionally strict: an existing target image is an error,
so an old VM can never be silently reused. For a deliberate replacement set
`[run].recreate_existing_images = true` temporarily.

`[run].only_vms = ["bunny06"]` can be used for a pilot clone/individualization.

### Phase 3 — individualize existing QCOW2 images

```bash
./scripts/mct-vm.py individualize
```

This command does **not** know or need the golden image. It works only on the
already cloned `bunnyXX.qcow2` images. For each selected classroom VM it:

1. boots the existing QCOW2 headless and waits for provisioning SSH;
2. copies the already reviewed `hosts/bunnyXX.nix` into the guest checkout;
3. runs `nixos-rebuild switch --flake ...#bunnyXX`;
4. reboots the guest into that individualized generation and verifies that the
   running hostname is now `bunnyXX`;
5. clones the public GitHub course mirror as a temporary bootstrap source,
   without student credentials;
6. configures Forgejo as the only final remote (`origin`) without contacting or
   logging into Forgejo, then removes the temporary GitHub remote and any GitHub
   tracking metadata; this applies to student **and** teacher VMs;
7. creates/selects the student's local branch (teacher remains on `master`);
8. runs the course repository `_config/setup.sh` for hooks and VS Code read-only
   protection;
9. validates hostname, Git/MCT identity, repository/branch state and the finalized
   Continue config;
10. optionally optimizes image size;
11. shuts down cleanly.

There is deliberately **no VS Code autostart or first-launch manipulation**.
Students start VS Code themselves in class. Student branches deliberately have
no upstream; the first Forgejo contact remains the student's own:

```bash
git pub
```

### Prepare deployment images and rollout

After all individualized QCOW2 images are complete:

```bash
./scripts/mct-vm.py prepare-images
./scripts/mct-vm.py update-csv
./scripts/mct-vm.py rollout
```

`prepare-images` converts QCOW2 -> VMDK -> VMDK.ZST. `update-csv` writes the
compressed filenames and SHA256 values into the active rollout CSV and updates
the mode-specific checksums file. `rollout` uses the values in `[rollout]` and
the temporary controls in `[run]`.

## First-boot bootstrap of NixOS-Bunny

The public repository is cloned automatically on first boot:

```text
https://github.com/BerndDonner/NixOS-Bunny.git
    -> /home/student/NixOS-Bunny
```

After a successful clone, `/etc/nixos` is replaced by a symlink to that working
copy, so plain `sudo nixos-rebuild switch` uses the checked-out flake and the
configuration matching the current hostname. An existing clone is never
automatically pulled or modified; the golden image remains a reviewed snapshot.

The service can be inspected or retried with:

```bash
systemctl status mct-bootstrap-nixos-bunny.service
sudo systemctl restart mct-bootstrap-nixos-bunny.service
```

## Main files

- `config.toml` — the single user-facing configuration/help surface for mct-vm
- `flake.nix` — NixOS configurations
- `modules/mct-vm.nix` — system/desktop/VM configuration
- `modules/home/student.nix` — Home Manager entry point for `student`
- `modules/home/modules/git.nix` — global Git defaults and recovery aliases
- `hosts/*.nix` — host-specific Git identity/course data
- `assets/continue/config.yaml` — final Continue configuration installed in phase 2b
- `scripts/mct_vm/golden.py` — phase-2 prepare/finalize automation
- `scripts/mct_vm/individualize.py` — phase-3 classroom individualization
- `scripts/mct-vm.py` — single entry point for image preparation and rollout
