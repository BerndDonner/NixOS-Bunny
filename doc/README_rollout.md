# VM Rollout

The integrated rollout is driven by `scripts/config/config.toml`; there are no
rollout command-line options.

## 1. Prepare deployment artifacts on NixOS

After individualization, create the compressed VMware images and their checksum
sidecars:

```text
./scripts/mct-vm.py prepare-images
```

For every active VM this produces a pair next to the prepared image source:

```text
bunny12.vmdk.zst
bunny12.vmdk.zst.sha256
```

The rollout CSV is deliberately **not modified**. It remains the authoritative
PC/VM/student mapping only. Filenames are derived from the VM name by the common
artifact-naming code; the expected SHA256 comes from the sidecar.

## 2. Stage the rollout SSD

Configure a dedicated directory on the mounted SSD:

```toml
[rollout]
prepared_images_dir = "images"
staging_dir = "/run/media/bernd/MCT-ROLLOUT/NixOS-Bunny"
windows_vm_directory = 'C:\Virtual_Machines'
windows_tools_dir = "tools"
```

`stage-rollout` always stages **all active VMs** from the active rollout CSV;
`[run].only_vms` is intentionally ignored for this command. Before copying it
verifies every source image against its sidecar. After copying it verifies every
image on the SSD again.

```text
./scripts/mct-vm.py stage-rollout
```

The staged directory is self-contained for Windows rollout and includes the
repository/tooling, `images/`, `tools/zstd.exe`, and the active rollout CSV.
The source `tools/zstd.exe` must therefore exist before staging.

## 3. Roll out from a Windows teacher PC

Requirements are Python 3.11+ and the standard Windows tools used by the
rollout (`robocopy`, `schtasks`, PowerShell/certutil, administrative shares).
Run Python directly from the SSD; no CMD wrapper is used:

```text
python scripts\mct-vm.py rollout
```

Before contacting any classroom PC, rollout verifies all selected local image +
sidecar pairs. A PC that fails the reachability check is an error, not a silent
success.

For a test run set `run.dry_run = true`. To target a single classroom PC, set
`run.only_pc` temporarily. To ignore an existing remote marker and redeploy,
set `run.redeploy_even_if_current = true` temporarily. Restore `[run]` to its
normal values afterwards.

Normal rollout copies the compressed image, verifies its SHA256 on the target,
writes the remote verification marker and unpacks remotely.
`rollout_without_verification = true` remains the emergency path and writes an
emergency manifest instead of claiming normal remote verification.
