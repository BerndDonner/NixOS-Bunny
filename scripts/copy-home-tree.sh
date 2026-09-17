#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  copy-home-tree.sh SOURCE_ROOT HOST_OR_USER_AT_HOST

Copy the *contents* of SOURCE_ROOT directly into the remote student's home
folder, preserving the directory tree (including dotfiles and symlinks).

Examples:
  ./scripts/copy-home-tree.sh /srv/mct/arduino-offline 192.168.10.42
  ./scripts/copy-home-tree.sh ~/mct-home-tree student@bunny

If only a host is supplied, the SSH user defaults to "student".
EOF
}

if [[ $# -ne 2 ]]; then
  usage >&2
  exit 2
fi

source_root=$1
remote=$2

if [[ ! -d "$source_root" ]]; then
  echo "ERROR: source root is not a directory: $source_root" >&2
  exit 2
fi

if [[ "$remote" != *@* ]]; then
  remote="student@$remote"
fi

echo "Copying contents of: $source_root"
echo "Into remote home:  $remote:~"

# Streaming a tar archive avoids all the usual recursive-scp corner cases and
# copies hidden files as well.  Extraction runs as the student, so the copied
# files are owned by the student without a later chown pass.
tar -C "$source_root" -cf - . | ssh "$remote" 'tar -C "$HOME" -xf -'

echo "Done."
