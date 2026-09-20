# VM Rollout

The integrated rollout is driven by the `scripts/config/config.toml`; there are
no rollout command-line options anymore.

Before a run, review especially:

```toml
[workflow]
mode = "classroom"

[rollout]
prepared_images_dir = "images"
windows_vm_directory = 'C:\Virtual_Machines'

[run]
rollout_include = ["*"]
rollout_exclude = []
dry_run = false
redeploy_even_if_current = false
extra_diagnostics = false
rollout_without_verification = false
```

Then run:

```text
python scripts\mct-vm.py rollout
```

For a test run set `run.dry_run = true`. Select targets with
`run.rollout_include` and `run.rollout_exclude`; patterns are case-insensitive
and support `*` and `?`. Includes are ORed and excludes are applied afterwards.
To ignore an existing marker and redeploy, set
`run.redeploy_even_if_current = true` temporarily. Restore the `[run]` section to
its normal values afterwards.

Normal rollout copies the compressed image, verifies its SHA256 on the target,
writes the marker and unpacks remotely. `rollout_without_verification = true` is
the emergency path and writes an emergency manifest instead of claiming normal
verification.
