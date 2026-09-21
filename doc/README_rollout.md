# VM Rollout

The integrated rollout is driven by `scripts/config/config.toml`; there are no
rollout command-line options.

## 1. Build deployment artifacts on NixOS

After `build-vms` has produced finished QCOW2/UEFI pairs, create the compressed
VMware images and checksum sidecars:

```text
./scripts/mct-vm.py build-rollout-images
```

For every selected active VM this produces the committed pair:

```text
bunny12.vmdk.zst
bunny12.vmdk.zst.sha256
```

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

`stage-rollout` always stages **all active VMs** from the active rollout CSV;
`[run].vms_include` / `vms_exclude` are intentionally ignored. It verifies the
source pairs, rebuilds the staged image set, and verifies the copied pairs again.

```text
./scripts/mct-vm.py stage-rollout
```

The staged directory is self-contained for Windows rollout and includes the
repository/tooling, `images/`, `scripts/tools/zstd.exe`, and the active rollout
CSV. Private/local repositories under `repos/` are explicitly excluded.

## 3. Roll out from a Windows teacher PC

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
