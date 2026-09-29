# VM Rollout

The integrated rollout is driven by `scripts/config/config.toml`; there are no
rollout command-line options.

## 1. Build deployment artifacts on NixOS

After `build-vms` has produced finished QCOW2/UEFI pairs, create the compressed
VMware images and checksum sidecars:

```text
./scripts/mct-vm.py build-rollout-images
```

For every selected active VM this produces the committed VMware pair:

```text
bunny12.vmdk.zst
bunny12.vmdk.zst.sha256
```

For a classroom amd64 VM listed as `linux-amd64` in
`scripts/config/private-devices.csv`, it additionally produces:

```text
bunny12.qcow2.zst
bunny12.qcow2.zst.sha256
```

The SHA256 sidecar always verifies the compressed `.zst` file.

The conversion uses temporary `.building.vmdk` / `.building.vmdk.zst` files.
The final image plus a valid sidecar is the success state; a later normal run
skips it. To deliberately regenerate these artifacts use:

```text
./scripts/mct-vm.py reset-rollout-images
./scripts/mct-vm.py build-rollout-images
```

The rollout CSV is deliberately not modified. Filenames are derived from the
VM name and the expected SHA256 comes from the sidecar.

## 2. Stage the rollout SSD

Configure a dedicated directory on the mounted SSD:

```toml
[rollout]
prepared_images_dir = "images"
staging_dir = "/run/media/bernd/MCT-ROLLOUT/NixOS-Bunny"
windows_vm_directory = 'C:\Virtual_Machines'
```

`stage-rollout` always stages **all active school VMs**; `[run].vms_include` /
`vms_exclude` are intentionally ignored. The root `images/` set is always the
amd64 Windows school rollout. In classroom mode, `private-devices.csv` is also
resolved against `rollout.csv` and a self-contained `private/` tree is staged
with the required amd64/arm64 artifacts.

```text
./scripts/mct-vm.py stage-rollout
```

The staged directory is self-contained for Windows rollout and includes the
repository/tooling, root `images/`, `scripts/tools/zstd.exe`, and the active
rollout CSV. In classroom mode it also contains `private/manifest.csv`,
`private/images/{amd64,arm64}/` and one `students/<forgejo>-<bunnyXX>/INFO.txt`
folder per student with private devices. Private/local repositories under
`repos/` are explicitly excluded.

## 3. Roll out from a Windows teacher PC

`rollout` is intentionally amd64-only. ARM64 artifacts on the SSD are private
device images and are never deployed to school PCs by this command.


Requirements are Python 3.11+ and the standard Windows tools used by rollout
(`robocopy`, `schtasks`, PowerShell/certutil, administrative shares):

```text
python scripts\mct-vm.py rollout
```

Before contacting any classroom PC, rollout verifies all selected local image +
sidecar pairs. A PC that fails the reachability check is an error.

For a test run set `run.dry_run = true`. `run.rollout_include` and
`run.rollout_exclude` select target rows. To ignore an existing remote SHA
marker and redeploy, set `run.redeploy_even_if_current = true` temporarily.

Normal rollout copies the compressed image, verifies its SHA256 on the target,
writes the remote verification marker and unpacks remotely.
`rollout_without_verification = true` remains the emergency path and writes an
emergency manifest instead of claiming normal remote verification.
