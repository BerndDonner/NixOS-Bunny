# NixOS-Bunny — MCT VM configuration

NixOS configuration for the Microcontrollertechnik classroom VMs.

The repository builds the generic/golden Bunny image and the later
host-specific `bunny00`, `bunny01`, ... configurations. It is intentionally
separate from the generated course repositories (`MCT_I3A`, `MCT_E3A`, ...):
Bunny provides OS/user/Git identity and recovery tooling; the course repository
provides `upmaster`,
pre-commit policy and VS Code course protection.

## Git identity model

`scripts/config/rollout.csv` contains the human identity, technical Forgejo login and course:

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
regenerate it from the current `scripts/config/rollout.csv` with `generate-hosts`.  The generator removes stale
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
`[provisioning].preparation_host_key` in `scripts/config/config.toml` (normally
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

The VM build and management-tool environment are separate flakes:

```bash
nix develop ./scripts
```

The management shell supplies Python and generic command-line dependencies. QEMU
and `qemu-img` intentionally come from the host so the shell does not shadow a
deliberately selected host QEMU version.

`mct-vm.py` has deliberately no command-line options. Persistent settings,
temporary selections and operational documentation live in
`scripts/config/config.toml`. The command line only chooses an explicit stage:

```bash
./scripts/mct-vm.py config-check
./scripts/mct-vm.py generate-hosts
./scripts/mct-vm.py build-golden
# manual work in the visible golden VM, then clean shutdown
./scripts/mct-vm.py finalize-golden
./scripts/mct-vm.py build-vms
./scripts/mct-vm.py build-rollout-images
./scripts/mct-vm.py stage-rollout
./scripts/mct-vm.py rollout
```

Intentional rollback is explicit and destructive only for derived artifacts:

```bash
./scripts/mct-vm.py reset-golden
./scripts/mct-vm.py reset-finalized-golden
./scripts/mct-vm.py reset-vms
./scripts/mct-vm.py reset-rollout-images
```

A `reset-*` command **never rebuilds anything**. It only removes the requested
result and everything locally derived from it. Rebuilding is always done with
the normal commands above. `reset-golden` is the only reset that creates a
backup first, because the manual golden contains work that is not reproducible.

`[workflow].mode` selects one output family. Classroom uses
`scripts/config/rollout.csv` and `bunnyXX.*`; lockdown uses
`scripts/config/rollout-lockdown.csv` and `bunnyXX-lockdown.*`. The lockdown CSV
is a deployment/exam selection and must be created/reviewed for the actual exam.
It is **not** used to generate `hosts/bunnyXX.nix`.

Temporary run controls remain under `[run]`. `vms_include` / `vms_exclude`
select rows for `build-vms`, `build-rollout-images` and their selected reset
commands; `rollout_include` / `rollout_exclude` affect Windows rollout only.
Patterns are case-insensitive `*`/`?` globs over CSV fields and full-name words.

### Artifact names are the build state

There is no second `.state` database. Final artifact names are success markers;
work-in-progress files use an explicit temporary infix:

```text
golden-26.05.building.qcow2    automatic preparation + manual GUI work
golden-26.05.qcow2             protected, powered-off manual golden
golden-26.05.finalizing.qcow2  disposable finalization copy
golden-26.05.finalized.qcow2   source accepted by build-vms

bunny00.building.qcow2         interrupted/in-progress VM build
bunny00.qcow2                  successfully built classroom VM
bunny00-lockdown.qcow2         successfully built lockdown VM

bunny00.building.vmdk.zst      in-progress rollout image
bunny00.vmdk.zst + .sha256     committed rollout artifact
```

A normal build command skips an already complete final output. To deliberately
start that stage again, run the corresponding `reset-*` command first. Stale
`.building`/`.finalizing` artifacts are disposable and are recreated from their
protected source.

### Host identity generation

`hosts/bunnyXX.nix` is generated **only** from the classroom identity mapping
`scripts/config/rollout.csv`:

```bash
./scripts/mct-vm.py generate-hosts
```

This is independent of `[workflow].mode`. A lockdown CSV selects which existing
student identities receive exam images; it must not replace or delete the
classroom host definitions.

### Build and manually prepare the golden image

```bash
./scripts/mct-vm.py build-golden
```

`build-golden` now owns the former external `nix build .#qcow2` step as well as
the former `prepare-golden` automation. It:

1. runs `nix build .#qcow2 --no-link --print-out-paths`;
2. copies the single QCOW2 build result to
   `golden-*.building.qcow2` using reflink/sparse copying where supported;
3. starts that image visibly with provisioning SSH;
4. overlays optional student-home content;
5. installs and verifies `assets/continue/config.yaml`;
6. leaves the VM running for the deliberate manual setup.

Perform the checklist in `doc/golden-manual-checklist.md`, then shut the visible
VM down cleanly. The `.building` file contains the manual work until
`finalize-golden` promotes it to the protected stable name.

### Finalize without touching the manual golden

```bash
./scripts/mct-vm.py finalize-golden
```

If no stable manual golden exists yet, a powered-off
`golden-*.building.qcow2` is renamed to the stable `golden-*.qcow2` pair. From
that point on the manual golden is protected: finalization always **copies**,
never renames or modifies, the stable manual QCOW2/UEFI pair:

```text
golden-26.05.qcow2
        | COPY
        v
golden-26.05.finalizing.qcow2
        | cleanup + Chromium policy + fstrim + clean shutdown
        v
golden-26.05.finalized.qcow2
```

A failed `.finalizing` copy is disposable; a retry starts again from the
protected manual golden. A successful existing `.finalized` pair is skipped.
Use `reset-finalized-golden` to remove it deliberately while preserving the
manual source.

### Build per-student VMs

```bash
./scripts/mct-vm.py build-vms
```

This replaces the old public `clone` + `individualize` split. For every selected
VM without a final QCOW2/UEFI pair it copies the finalized golden to a
`.building` pair, provisions and validates it, shuts it down, and only then
renames the pair to the final `bunnyXX*` names. A final pair therefore means the
whole VM build succeeded. A stale `.building` pair from an interrupted run is
recreated from the finalized golden.

For classroom mode, `build-vms` keeps the previous course workflow: host-specific
Nix configuration, reboot, public GitHub bootstrap, Forgejo as the only final
remote, student branch/setup hooks/VS Code protection, validation and shutdown.

For lockdown mode, `scripts/config/rollout-lockdown.csv` selects the VMs while
`hosts/bunnyXX.nix` still comes from the classroom identity mapping. The exam
repository is configured with:

```toml
[lockdown]
repo = "repos/MCT-Schulaufgabe1"
```

`repos/` is ignored by the outer repository and excluded from rollout staging.
The configured exam repo must itself be a clean Git repository with a named
current branch. `build-vms` creates a Git bundle locally and copies that bundle
into each exam VM; no public Git server and no Forgejo credentials are needed.
The finished exam repository receives exactly one HTTPS `origin`, derived from
the active lockdown repository name plus the student's Forgejo login. No
credentials are provisioned into the image; KDE/KWallet handles the student's
normal HTTPS credentials when the first push is made.

Lockdown provisioning first boots the ordinary host-specific generation so
identity and unrestricted provisioning work normally. The exam repository is
installed before the firewall is activated. The **last SSH session** switches to
`bunnyXX-lockdown`, removes older NixOS system generations, rewrites the boot
configuration, validates that exactly one system generation remains and that
`mct-exam-firewall` is enabled/active, then powers the VM off. This prevents a
student from selecting an older non-lockdown generation from the bootloader.

In the final lockdown generation `student` is **not** a member of `wheel`.
The only privileged fallback operation granted through sudo is stopping the
exam firewall, and it requires the per-VM fallback password generated during
`build-vms`.

### Fallback submission: deliberately open the network

Normal exam submission is Git over HTTPS. If that path is unavailable, the
teacher can give the affected student the VM-specific fallback password. The
student then runs exactly:

```bash
sudo /run/current-system/sw/bin/systemctl stop mct-exam-firewall.service
```

This intentionally flushes the lockdown nftables ruleset and opens the network.
The command is printed prominently whenever the student opens a new interactive
Bash terminal in a lockdown VM, so there is no separate fallback file to find.

Passwords are generated separately for each VM and each configured exam repo in
a deliberately easy-to-read form such as `Birke-Mond-47`: two simple words and
two digits. They are **not** stored in Git, `config.toml`, the Nix store, or the
per-VM build log. The teacher-side mapping is stored with mode `0600` at:

```text
.mct-vm/lockdown-passwords/<Prüfungsbezeichnung>.csv
```

Keep that file available until the exam and distribute a password only when the
fallback is actually needed. Rebuilding the same VM for the same exam reuses its
existing password; changing the exam repository name creates a separate password
file.

Existing lockdown images built before this hardening must be rebuilt (`reset-vms`
then `build-vms` in lockdown mode); changing the repository alone cannot remove
`wheel` from an already-built image.

### Create per-student Forgejo exam repositories

Forgejo host and exam owner are configured once in the same `config.toml` used
by `mct-vm` and `forgejo-exam`:

```toml
[forgejo]
host = "forgejo.meisterk.de"
exam_owner = "donner"
```

The Forgejo repository name is derived from the local lockdown repository name.
If `[lockdown].repo` is:

```toml
[lockdown]
repo = "repos/MCT-Schulaufgabe1"
```

and a row in `scripts/config/rollout-lockdown.csv` contains Forgejo login
`mayr`, the personal remote repository is:

```text
https://forgejo.meisterk.de/donner/MCT-Schulaufgabe1_mayr.git
```

`rollout-lockdown.csv` has the **same columns** as `rollout.csv`, but is a
separate mapping because exam seating can use different `pcname` values.  The
exam script reads only active rows from the lockdown CSV; a teacher row whose
Forgejo login equals the repository owner (`donner`) is skipped.

Preview the planned repositories first:

```bash
python3 scripts/forgejo-exam.py create-repos --dry-run
```

Then create missing repositories:

```bash
python3 scripts/forgejo-exam.py create-repos
```

The API token is read from `FORGEJO_TOKEN` when set, otherwise it is requested
without echoing.  This step is deliberately idempotent: an existing private
repository is kept, while an existing non-private repository causes an error.
New repositories are **private and empty**. This command does not grant student
access and does not push exam content. The lockdown VM already receives the
committed local exam repository via Git bundle and gets the matching personal
HTTPS `origin` during `build-vms`.

Immediately before the exam, grant every student write access only to their own
personal exam repository:

```bash
python3 scripts/forgejo-exam.py grant --dry-run
python3 scripts/forgejo-exam.py grant
```

After the exam, remove those collaborator permissions again:

```bash
python3 scripts/forgejo-exam.py revoke --dry-run
python3 scripts/forgejo-exam.py revoke
```

All three Forgejo commands use the same `[lockdown].repo`, `[forgejo]` settings
and `rollout-lockdown.csv`; there is no separate exam-name mapping to keep in
sync.

The lockdown firewall permits DNS/NTP plus only the configured exam services:
`ai.donner-lab.org:11434` and `${forgejo.host}:443`. The service resolves all
current IPv4/IPv6 addresses at startup and installs exact address/port rules; a
failed resolution leaves that endpoint blocked rather than opening the network.

### Build rollout images

```bash
./scripts/mct-vm.py build-rollout-images
```

For each selected finished QCOW2 this creates a temporary VMDK, compresses it to
`.building.vmdk.zst`, then publishes the final `.vmdk.zst` and atomically writes
its `.sha256` sidecar. The image + valid sidecar pair is the success marker and
is skipped on later runs. A lone final ZST without a sidecar is treated as an
interrupted build and rebuilt; a final pair with a checksum mismatch is an
error.

The temporary VMDK is removed after successful compression. Old legacy `.vmdk`
files are removed by `reset-rollout-images` but are never trusted as build state.

### Reset semantics

- `reset-rollout-images`: selected VMs only; removes VMDK/ZST/SHA and temporary
  rollout files. Finished QCOW2/UEFI images remain.
- `reset-vms`: selected VMs only; removes their final/in-progress VM images and
  rollout artifacts. The finalized golden remains.
- `reset-finalized-golden`: keeps the protected manual golden, removes
  `.finalizing`/`.finalized` and all locally derived classroom/lockdown VM and
  rollout artifacts.
- `reset-golden`: backs up any stable/in-progress manual golden under
  `VM_DIR/backups/golden/<timestamp>/`, removes finalized and all downstream
  local artifacts, and leaves rebuilding to a later `build-golden` command.

Reset commands do not delete already staged SSD contents or touch remote school
PCs. After rebuilding upstream artifacts, run `stage-rollout` and `rollout`
again as appropriate.

### Stage and deploy

```bash
./scripts/mct-vm.py stage-rollout
./scripts/mct-vm.py rollout
```

`stage-rollout` verifies every active image/sidecar pair and rebuilds the staged
image set on the configured SSD. It deliberately ignores `vms_include` /
`vms_exclude` so a pilot filter cannot silently produce an incomplete rollout
medium. Private `repos/` content is explicitly excluded from the staged tree.

On the Windows teacher PC:

```text
python scripts\mct-vm.py rollout
```

`rollout` retains its existing SHA-marker logic. `redeploy_even_if_current`
remains a rollout-only control; the old persistent `recreate_existing_images`
option has been removed.

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

- `scripts/config/config.toml` — the single user-facing configuration/help surface for mct-vm
- `flake.nix` — NixOS configurations
- `modules/mct-vm.nix` — system/desktop/VM configuration
- `modules/home/student.nix` — Home Manager entry point for `student`
- `modules/home/modules/git.nix` — global Git defaults and recovery aliases
- `hosts/*.nix` — host-specific Git identity/course data
- `assets/continue/config.yaml` — reviewed Continue configuration installed before the manual phase
- `scripts/mct_vm/golden.py` — golden build/finalization and protected-copy lifecycle
- `scripts/mct_vm/vm_build.py` — atomic per-VM classroom/lockdown builds
- `scripts/mct_vm/artifacts.py` — final/temporary VM artifact names and checksum sidecars
- `scripts/mct_vm/reset.py` — explicit artifact rollback without automatic rebuilding
- `scripts/mct_vm/stage.py` — verified self-contained rollout-SSD staging
- `scripts/mct-vm.py` — single entry point for image preparation and rollout
- `scripts/flake.nix` — separate development shell for the management tools
- `scripts/config/rollout.csv` — authoritative classroom assignment/inventory input
