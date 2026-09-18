#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  copy-home-tree.sh [--key PRIVATE_KEY] SOURCE_ROOT HOST_OR_USER_AT_HOST[:PORT]

Overlay the regular files below SOURCE_ROOT onto the remote student's home.

Semantics:
  - hidden files and hidden directories are included;
  - only regular files are copied (no symlinks, no empty directories);
  - missing parent directories are created as needed;
  - existing directories are kept and their other contents are never deleted;
  - existing regular files are overwritten;
  - ~/.continue/config.yaml is deliberately never copied by this script.

Options:
  --key PRIVATE_KEY
      Use this SSH private key for the connection.
      The script also sets IdentitiesOnly=yes so no other SSH identities are tried.

  -h, --help
      Show this help.

Examples:
  ./scripts/copy-home-tree.sh /srv/mct/golden-home 192.168.10.42
  ./scripts/copy-home-tree.sh ~/mct-home-tree student@bunny
  ./scripts/copy-home-tree.sh ~/mct-home-tree localhost:2222
  ./scripts/copy-home-tree.sh ~/mct-home-tree student@localhost:2222
  ./scripts/copy-home-tree.sh --key ~/.ssh/bernd_tracy \
      ~/mct-home-tree student@localhost:2222

If only a host is supplied, the SSH user defaults to "student".
If no port is supplied, SSH uses its normal default port (22).

The HOST:PORT form is especially useful with QEMU user networking, e.g.
when run-qemu.sh was started with:

  ./scripts/run-qemu.sh --ssh golden.qcow2

Then use:

  ./scripts/copy-home-tree.sh --key ~/.ssh/bernd_tracy \
      ~/mct-home-tree localhost:2222
USAGE
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

key_file=""

POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --key)
      [[ $# -ge 2 ]] || die "--key requires a private-key path"
      key_file=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      POSITIONAL+=("$@")
      break
      ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ ${#POSITIONAL[@]} -ne 2 ]]; then
  usage >&2
  exit 2
fi

source_root=${POSITIONAL[0]}
remote_spec=${POSITIONAL[1]}
continue_config='.continue/config.yaml'

if [[ ! -d "$source_root" ]]; then
  die "source root is not a directory: $source_root"
fi

if [[ -n "$key_file" ]]; then
  if [[ ! -r "$key_file" ]]; then
    die "SSH private key is not readable: $key_file"
  fi
  key_file="$(realpath "$key_file")"
fi

# Parse [USER@]HOST[:PORT].
#
# This intentionally targets ordinary host names / IPv4 addresses and the
# localhost:PORT form used by QEMU port forwarding. Raw IPv6 literals are not
# accepted here because ':' is used as the optional port separator.
port=""
remote="$remote_spec"

if [[ "$remote_spec" == *:* ]]; then
  port="${remote_spec##*:}"
  remote="${remote_spec%:*}"

  if [[ -z "$remote" || ! "$port" =~ ^[0-9]+$ || "$port" -lt 1 || "$port" -gt 65535 ]]; then
    die "invalid HOST:PORT specification: $remote_spec"
  fi
fi

if [[ "$remote" != *@* ]]; then
  remote="student@$remote"
fi

ssh_cmd=(ssh)

if [[ -n "$key_file" ]]; then
  ssh_cmd+=(
    -o IdentitiesOnly=yes
    -i "$key_file"
  )
fi

if [[ -n "$port" ]]; then
  ssh_cmd+=(-p "$port")
fi

ssh_cmd+=("$remote")

echo "Overlaying regular files from: $source_root"
if [[ -n "$port" ]]; then
  echo "Into remote home:             $remote:~ (SSH port $port)"
else
  echo "Into remote home:             $remote:~"
fi

if [[ -n "$key_file" ]]; then
  echo "Using SSH key:                $key_file"
fi

# Copy only regular files. Using find means dotfiles/dot-directories are
# included automatically. Directory entries themselves are deliberately not
# archived, so existing directory metadata/content is not replaced or pruned.
# Continue configuration is intentionally outside this workflow.  Even when a
# source tree contains .continue/config.yaml, never copy it into the VM.
(
  cd "$source_root"
  find . -type f ! -path "./$continue_config" -print0 \
    | tar --null --files-from=- -cf -
) | "${ssh_cmd[@]}" 'tar -C "$HOME" -xf -'

if [[ -f "$source_root/$continue_config" ]]; then
  echo "INFO: skipping $continue_config by policy." >&2
fi

echo "Done. No remote files or directories were deleted."
