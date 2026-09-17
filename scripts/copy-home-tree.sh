#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  copy-home-tree.sh SOURCE_ROOT HOST_OR_USER_AT_HOST

Overlay the regular files below SOURCE_ROOT onto the remote student's home.

Semantics:
  - hidden files and hidden directories are included;
  - only regular files are copied (no symlinks, no empty directories);
  - missing parent directories are created as needed;
  - existing directories are kept and their other contents are never deleted;
  - existing files are overwritten, except ~/.continue/config.yaml;
  - ~/.continue/config.yaml is copied only when it does not already exist.
    If it exists, the script prints a warning and leaves it untouched.

Examples:
  ./scripts/copy-home-tree.sh /srv/mct/golden-home 192.168.10.42
  ./scripts/copy-home-tree.sh ~/mct-home-tree student@bunny

If only a host is supplied, the SSH user defaults to "student".
USAGE
}

if [[ $# -ne 2 ]]; then
  usage >&2
  exit 2
fi

source_root=$1
remote=$2
continue_config='.continue/config.yaml'

if [[ ! -d "$source_root" ]]; then
  echo "ERROR: source root is not a directory: $source_root" >&2
  exit 2
fi

if [[ "$remote" != *@* ]]; then
  remote="student@$remote"
fi

echo "Overlaying regular files from: $source_root"
echo "Into remote home:             $remote:~"

# Copy only regular files.  Using find means dotfiles/dot-directories are
# included automatically.  Directory entries themselves are deliberately not
# archived, so existing directory metadata/content is not replaced or pruned.
# The Continue config is handled separately below.
(
  cd "$source_root"
  find . -type f ! -path "./$continue_config" -print0 \
    | tar --null --files-from=- -cf -
) | ssh "$remote" 'tar -C "$HOME" -xf -'

# Continue creates a default ~/.continue/config.yaml on first use.  We do not
# want to overwrite that silently: for the golden-image workflow the default
# file should be deleted intentionally first.  Then rerunning this script will
# install the prepared config.yaml from SOURCE_ROOT.
if [[ -f "$source_root/$continue_config" ]]; then
  if ssh "$remote" 'test -e "$HOME/.continue/config.yaml"'; then
    echo "WARNING: $remote:~/.continue/config.yaml already exists; leaving it untouched." >&2
    echo "         Delete the default config intentionally and rerun this script to install the prepared one." >&2
  else
    echo "Installing prepared ~/.continue/config.yaml"
    (
      cd "$source_root"
      tar -cf - "$continue_config"
    ) | ssh "$remote" 'tar -C "$HOME" -xf -'
  fi
fi

echo "Done. No remote files or directories were deleted."
