#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  run-qemu.sh [OPTIONS] <disk.qcow2>

Examples:
  ./run-qemu.sh MCT_I3A_Unterricht01.qcow2
  ./run-qemu.sh -m 8192 -c 8 ./nixos.qcow2
  DISK_QCOW2=./nixos.qcow2 VARS_FD=./OVMF_VARS.fd ./run-qemu.sh ./nixos.qcow2

Options:
  -m, --mem <MB>        RAM in MiB (default: 4096)
  -c, --cores <N>       CPU cores (default: 4)
  --vars <path>         Path to OVMF VARS file (default: <disk>.OVMF_VARS.fd)
  --no-kvm              Disable KVM acceleration
  --dry-run             Print qemu command and exit
  -h, --help            Show this help

Environment (optional overrides):
  DISK_QCOW2            Default disk image path if <disk.qcow2> is omitted
  VARS_FD               Default VARS file path
  MEM_MB                Default RAM in MiB
  CORES                 Default CPU cores

EOF
}

# --- Defaults (overridable via env) ---
DISK_QCOW2="${DISK_QCOW2:-./nixos.qcow2}"
VARS_FD="${VARS_FD:-}"
MEM_MB="${MEM_MB:-4096}"
CORES="${CORES:-4}"

ACCEL_ARGS=(-accel kvm -cpu host)
DRY_RUN=0

# --- Parse args ---
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--mem)
      [[ $# -ge 2 ]] || { echo "ERROR: $1 requires a value" >&2; exit 2; }
      MEM_MB="$2"; shift 2
      ;;
    -c|--cores)
      [[ $# -ge 2 ]] || { echo "ERROR: $1 requires a value" >&2; exit 2; }
      CORES="$2"; shift 2
      ;;
    --vars)
      [[ $# -ge 2 ]] || { echo "ERROR: $1 requires a value" >&2; exit 2; }
      VARS_FD="$2"; shift 2
      ;;
    --no-kvm)
      ACCEL_ARGS=(-cpu host)  # fall back to TCG if no -accel given
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
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
      echo "ERROR: Unknown option: $1" >&2
      echo >&2
      usage >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

# Disk image: prefer positional arg, else env default
if [[ ${#POSITIONAL[@]} -ge 1 ]]; then
  DISK_QCOW2="${POSITIONAL[0]}"
fi

if [[ -z "${DISK_QCOW2:-}" ]]; then
  echo "ERROR: Missing disk image argument." >&2
  echo >&2
  usage >&2
  exit 2
fi

# Derive VARS path from disk *after* DISK_QCOW2 is final (unless user set it)
if [[ -z "${VARS_FD:-}" ]]; then
  VARS_FD="${DISK_QCOW2%.*}.OVMF_VARS.fd"
fi

# --- Resolve OVMF from Nixpkgs ---
OVMF_FV="$(nix eval --raw nixpkgs#OVMF.fd.outPath)/FV"
OVMF_CODE="$OVMF_FV/OVMF_CODE.fd"
OVMF_VARS_TEMPLATE="$OVMF_FV/OVMF_VARS.fd"

if [[ ! -r "$DISK_QCOW2" ]]; then
  echo "ERROR: Disk image not found/readable: $DISK_QCOW2" >&2
  exit 1
fi

if [[ ! -r "$OVMF_CODE" ]]; then
  echo "ERROR: OVMF_CODE.fd not found: $OVMF_CODE" >&2
  exit 1
fi

if [[ -e "$VARS_FD" && ! -w "$VARS_FD" ]]; then
  echo "ERROR: VARS file exists but is not writable: $VARS_FD" >&2
  echo "Hint: try 'sudo chown $USER:$USER \"$VARS_FD\"' or choose --vars <path>" >&2
  exit 1
fi

# Create a writable VARS file once (persistent UEFI NVRAM)
if [[ ! -e "$VARS_FD" ]]; then
  echo "Creating VARS file: $VARS_FD"
  cp -n "$OVMF_VARS_TEMPLATE" "$VARS_FD"
fi

QEMU_CMD=(
  qemu-system-x86_64
  "${ACCEL_ARGS[@]}"
  -m "$MEM_MB" -smp "cores=$CORES,threads=1,sockets=1" -machine q35
  -boot order=c
  -device qemu-xhci -device usb-tablet
  -device virtio-vga-gl -display sdl,gl=on
  -netdev user,id=n1 -device virtio-net-pci,netdev=n1
  -drive "file=$DISK_QCOW2,if=virtio,format=qcow2"
  -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE"
  -drive "if=pflash,format=raw,file=$VARS_FD"
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would run:\n'
  printf '  %q' "${QEMU_CMD[@]}"
  printf '\n'
  exit 0
fi

exec "${QEMU_CMD[@]}"
