#!/usr/bin/env bash
set -euo pipefail

# Bunny QEMU runner for amd64 and arm64 guests.
#
# Architecture and acceleration are separate concepts:
#   --arch amd64|arm64  selects the guest architecture.
#
# Acceleration is chosen automatically:
#   host arch == guest arch and usable /dev/kvm -> KVM + -cpu host
#   otherwise                                  -> TCG + -cpu max
#
# GUI mode always uses the best known virtio/virgl path for the selected
# architecture. --headless removes the graphical device entirely.

DEFAULT_MEM_MB=4096
DEFAULT_CORES=4
DEFAULT_SSH_PORT=2222
DEFAULT_ARCH="amd64"
SHARE_TAG="hostshare"

usage() {
  cat <<'EOF'
Usage:
  run-qemu.sh [OPTIONS] <disk.qcow2>

Examples:
  ./run-qemu.sh golden.qcow2
  ./run-qemu.sh --arch arm64 ../result/bunny-arm.qcow2
  ./run-qemu.sh --arch=arm64 --snapshot ../result/bunny-arm.qcow2
  ./run-qemu.sh --arch arm64 --snapshot --arduino ../result/bunny-arm.qcow2
  ./run-qemu.sh --arch arm64 --ssh bunny-arm-dev.qcow2
  ./run-qemu.sh --arduino golden.qcow2
  ./run-qemu.sh --ssh --share ~/exam golden.qcow2

Options:
  --arch <ARCH>         Guest architecture: amd64 | arm64
                        (default: amd64)
                        --arch=arm64 is also accepted.

  -m, --mem <MB>        RAM in MiB (default: 4096)
  -c, --cores <N>       vCPU count (default: 4)
  --vars <PATH>         Writable UEFI VARS file
                        amd64 default: <disk>.OVMF_VARS.fd
                        arm64 default: <disk>.AARCH64_VARS.fd

  --arduino             Pass an official Arduino Uno R3 (2341:0043) through.
  --ssh                 Forward host localhost:2222 to guest SSH port 22.
  --port <PORT>         Host port for --ssh (default: 2222).
  --share <DIRECTORY>   Share one host directory through virtio-9p.
  --headless            Run without a graphical device/window.
  --discard             Pass discard/TRIM through to the qcow2 image.
  --snapshot            Keep disk and UEFI writes temporary.
                        Useful for read-only Nix-store images.
  --dry-run             Print the resulting QEMU command and exit.
  -h, --help            Show this help.

Environment overrides:
  DISK_QCOW2            Default disk image if no positional image is given.
  VARS_FD               Default UEFI VARS file.
  ARCH                  Default guest architecture: amd64 | arm64.
  MEM_MB                Default RAM in MiB.
  CORES                 Default vCPU count.
  SSH_PORT              Default host SSH port.

Automatic acceleration:
  amd64 host + amd64 guest + usable /dev/kvm -> KVM
  arm64 host + arm64 guest + usable /dev/kvm -> KVM
  otherwise                                  -> TCG

Automatic GUI:
  amd64 -> virtio-vga-gl     + SDL/OpenGL
  arm64 -> virtio-gpu-gl-pci + SDL/OpenGL

There is intentionally no --no-kvm and no --graphics option.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

normalize_host_arch() {
  case "$(uname -m)" in
    x86_64)  echo "amd64" ;;
    aarch64) echo "arm64" ;;
    *)
      echo "unknown"
      ;;
  esac
}

resolve_full_qemu() {
  echo "Resolving nixpkgs#qemu ..." >&2
  nix build --no-link --print-out-paths nixpkgs#qemu | tail -n1
}

# --- Defaults ---
DISK_QCOW2="${DISK_QCOW2:-}"
VARS_FD="${VARS_FD:-}"
ARCH="${ARCH:-$DEFAULT_ARCH}"
MEM_MB="${MEM_MB:-$DEFAULT_MEM_MB}"
CORES="${CORES:-$DEFAULT_CORES}"
SSH_PORT="${SSH_PORT:-$DEFAULT_SSH_PORT}"

ARDUINO=0
SSH_ENABLED=0
SHARE_DIR=""
HEADLESS=0
DISCARD=0
SNAPSHOT=0
DRY_RUN=0
PORT_WAS_SET=0

POSITIONAL=()

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch)
      [[ $# -ge 2 ]] || die "--arch requires amd64 or arm64"
      ARCH="$2"
      shift 2
      ;;

    --arch=*)
      ARCH="${1#--arch=}"
      shift
      ;;

    -m|--mem)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      MEM_MB="$2"
      shift 2
      ;;

    -c|--cores)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      CORES="$2"
      shift 2
      ;;

    --vars)
      [[ $# -ge 2 ]] || die "--vars requires a value"
      VARS_FD="$2"
      shift 2
      ;;

    --arduino)
      ARDUINO=1
      shift
      ;;

    --ssh)
      SSH_ENABLED=1
      shift
      ;;

    --port)
      [[ $# -ge 2 ]] || die "--port requires a value"
      SSH_PORT="$2"
      PORT_WAS_SET=1
      shift 2
      ;;

    --share)
      [[ $# -ge 2 ]] || die "--share requires a directory"
      SHARE_DIR="$2"
      shift 2
      ;;

    --headless)
      HEADLESS=1
      shift
      ;;

    --discard)
      DISCARD=1
      shift
      ;;

    --snapshot)
      SNAPSHOT=1
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
      die "Unknown option: $1"
      ;;

    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

# --- Validate ---
case "$ARCH" in
  amd64|arm64) ;;
  *) die "--arch must be 'amd64' or 'arm64'" ;;
esac

is_positive_integer "$MEM_MB" || die "--mem must be a positive integer"
is_positive_integer "$CORES" || die "--cores must be a positive integer"

if ! is_positive_integer "$SSH_PORT" || (( SSH_PORT > 65535 )); then
  die "--port must be an integer between 1 and 65535"
fi

if [[ "$PORT_WAS_SET" -eq 1 && "$SSH_ENABLED" -eq 0 ]]; then
  die "--port is valid only together with --ssh"
fi

if [[ -n "$SHARE_DIR" ]]; then
  [[ -d "$SHARE_DIR" ]] || die "Shared directory does not exist: $SHARE_DIR"
  SHARE_DIR="$(realpath "$SHARE_DIR")"
fi

if [[ ${#POSITIONAL[@]} -gt 1 ]]; then
  die "Too many positional arguments"
fi

if [[ ${#POSITIONAL[@]} -eq 1 ]]; then
  DISK_QCOW2="${POSITIONAL[0]}"
fi

[[ -n "$DISK_QCOW2" ]] || die "Missing disk image argument"
DISK_QCOW2="$(realpath -e "$DISK_QCOW2")"
[[ -r "$DISK_QCOW2" ]] || die "Disk image is not readable: $DISK_QCOW2"

# --- Host architecture and accelerator ---
HOST_ARCH="$(normalize_host_arch)"

if [[ "$HOST_ARCH" == "$ARCH" && -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; then
  ACCEL="kvm"
  CPU_MODEL="host"
else
  ACCEL="tcg"
  CPU_MODEL="max"
fi

# --- Network and disk ---
NETDEV="user,id=n1"
if [[ "$SSH_ENABLED" -eq 1 ]]; then
  NETDEV+=",hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22"
fi

DISK_DRIVE="file=$DISK_QCOW2,if=virtio,format=qcow2"
if [[ "$DISCARD" -eq 1 ]]; then
  DISK_DRIVE+=",discard=unmap,detect-zeroes=unmap"
fi
if [[ "$SNAPSHOT" -eq 1 ]]; then
  DISK_DRIVE+=",snapshot=on"
fi

# --- Resolve QEMU binary ---
QEMU_ROOT=""

case "$ARCH" in
  amd64)
    QEMU_BIN="$(command -v qemu-system-x86_64 || true)"
    if [[ -z "$QEMU_BIN" ]]; then
      QEMU_ROOT="$(resolve_full_qemu)"
      QEMU_BIN="$QEMU_ROOT/bin/qemu-system-x86_64"
    fi
    ;;
  arm64)
    QEMU_BIN="$(command -v qemu-system-aarch64 || true)"
    if [[ -z "$QEMU_BIN" ]]; then
      QEMU_ROOT="$(resolve_full_qemu)"
      QEMU_BIN="$QEMU_ROOT/bin/qemu-system-aarch64"
    fi
    ;;
esac

[[ -x "$QEMU_BIN" ]] || die "QEMU binary not found for guest architecture $ARCH"

# --- Architecture-specific QEMU + UEFI setup ---
QEMU_CMD=()

if [[ "$ARCH" == "amd64" ]]; then
  OVMF_FV="$(nix eval --raw nixpkgs#OVMF.fd.outPath)/FV"
  FW_CODE="$OVMF_FV/OVMF_CODE.fd"
  FW_VARS_TEMPLATE="$OVMF_FV/OVMF_VARS.fd"

  [[ -r "$FW_CODE" ]] || die "OVMF_CODE.fd not found: $FW_CODE"
  [[ -r "$FW_VARS_TEMPLATE" ]] || die "OVMF_VARS.fd not found: $FW_VARS_TEMPLATE"

  if [[ "$SNAPSHOT" -eq 0 ]]; then
    if [[ -z "$VARS_FD" ]]; then
      VARS_FD="${DISK_QCOW2%.*}.OVMF_VARS.fd"
    fi

    if [[ ! -e "$VARS_FD" ]]; then
      cp "$FW_VARS_TEMPLATE" "$VARS_FD"
      chmod u+w "$VARS_FD"
    fi
    [[ -w "$VARS_FD" ]] || die "UEFI VARS file is not writable: $VARS_FD"
  fi

  if [[ "$ACCEL" == "kvm" ]]; then
    ACCEL_ARGS=(-accel kvm -cpu host)
  else
    ACCEL_ARGS=(-accel "tcg,thread=multi" -cpu max)
  fi

  QEMU_CMD=(
    "$QEMU_BIN"
    -name "bunny-amd64"
    "${ACCEL_ARGS[@]}"
    -m "$MEM_MB"
    -smp "cores=$CORES,threads=1,sockets=1"
    -machine q35
    -boot order=c

    -device "qemu-xhci,id=xhci"
  )

  if [[ "$HEADLESS" -eq 1 ]]; then
    QEMU_CMD+=( -display none )
  else
    QEMU_CMD+=(
      -device virtio-vga-gl
      -device "usb-kbd,bus=xhci.0"
      -device "usb-tablet,bus=xhci.0"
      -display "sdl,gl=on"
    )
  fi

  QEMU_CMD+=(
    -netdev "$NETDEV"
    -device "virtio-net-pci,netdev=n1"
    -drive "$DISK_DRIVE"
    -drive "if=pflash,format=raw,readonly=on,file=$FW_CODE"
  )

  if [[ "$SNAPSHOT" -eq 1 ]]; then
    QEMU_CMD+=(
      -drive "if=pflash,format=raw,file=$FW_VARS_TEMPLATE,snapshot=on"
    )
  else
    QEMU_CMD+=(
      -drive "if=pflash,format=raw,file=$VARS_FD"
    )
  fi

else
  # ARM64 firmware is shipped with the full QEMU package. If the selected
  # qemu-system-aarch64 came from PATH, locate the package data directory
  # from its Nix store prefix when possible; otherwise resolve nixpkgs#qemu.
  if [[ -n "$QEMU_ROOT" ]]; then
    QEMU_DATADIR="$QEMU_ROOT/share/qemu"
  else
    QEMU_REAL="$(readlink -f "$QEMU_BIN")"
    QEMU_PREFIX="$(dirname "$(dirname "$QEMU_REAL")")"
    if [[ -d "$QEMU_PREFIX/share/qemu" ]]; then
      QEMU_DATADIR="$QEMU_PREFIX/share/qemu"
    else
      QEMU_ROOT="$(resolve_full_qemu)"
      QEMU_BIN="$QEMU_ROOT/bin/qemu-system-aarch64"
      QEMU_DATADIR="$QEMU_ROOT/share/qemu"
    fi
  fi

  FW_CODE="$QEMU_DATADIR/edk2-aarch64-code.fd"
  [[ -r "$FW_CODE" ]] || die "AArch64 UEFI code firmware not found: $FW_CODE"

  FW_VARS_TEMPLATE=""
  for candidate in \
    "$QEMU_DATADIR/edk2-aarch64-vars.fd" \
    "$QEMU_DATADIR/edk2-arm-vars.fd"; do
    if [[ -r "$candidate" ]]; then
      FW_VARS_TEMPLATE="$candidate"
      break
    fi
  done
  [[ -n "$FW_VARS_TEMPLATE" ]] || \
    die "AArch64 UEFI VARS template not found in $QEMU_DATADIR"

  if [[ "$SNAPSHOT" -eq 0 ]]; then
    if [[ -z "$VARS_FD" ]]; then
      VARS_FD="${DISK_QCOW2%.*}.AARCH64_VARS.fd"
    fi

    if [[ ! -e "$VARS_FD" ]]; then
      cp "$FW_VARS_TEMPLATE" "$VARS_FD"
      chmod u+w "$VARS_FD"
    fi
    [[ -w "$VARS_FD" ]] || die "AArch64 UEFI VARS file is not writable: $VARS_FD"
  fi

  if [[ "$ACCEL" == "kvm" ]]; then
    ACCEL_ARGS=(-accel kvm -cpu host)
  else
    ACCEL_ARGS=(-accel "tcg,thread=multi" -cpu max)
  fi

  ARM_DISK_DRIVE="file=$DISK_QCOW2,if=none,id=disk0,format=qcow2"
  if [[ "$DISCARD" -eq 1 ]]; then
    ARM_DISK_DRIVE+=",discard=unmap,detect-zeroes=unmap"
  fi
  if [[ "$SNAPSHOT" -eq 1 ]]; then
    ARM_DISK_DRIVE+=",snapshot=on"
  fi

  QEMU_CMD=(
    "$QEMU_BIN"
    -name "bunny-arm64"
    -machine virt
    "${ACCEL_ARGS[@]}"
    -m "$MEM_MB"
    -smp "cores=$CORES,threads=1,sockets=1"
    -nodefaults
    -no-reboot

    -drive "if=pflash,format=raw,readonly=on,file=$FW_CODE"
  )

  if [[ "$SNAPSHOT" -eq 1 ]]; then
    QEMU_CMD+=(
      -drive "if=pflash,format=raw,file=$FW_VARS_TEMPLATE,snapshot=on"
    )
  else
    QEMU_CMD+=(
      -drive "if=pflash,format=raw,file=$VARS_FD"
    )
  fi

  QEMU_CMD+=(
    -drive "$ARM_DISK_DRIVE"
    -device "virtio-blk-pci,drive=disk0,bootindex=1"

    -netdev "$NETDEV"
    -device "virtio-net-pci,netdev=n1,bootindex=2"

    -object "rng-random,filename=/dev/urandom,id=rng0"
    -device "virtio-rng-pci,rng=rng0"

    -device "qemu-xhci,id=xhci"
  )

  if [[ "$HEADLESS" -eq 1 ]]; then
    QEMU_CMD+=( -display none )
  else
    if ! "$QEMU_BIN" -device virtio-gpu-gl-pci,help >/dev/null 2>&1; then
      die "This QEMU build does not provide virtio-gpu-gl-pci"
    fi
    QEMU_CMD+=(
      -device virtio-gpu-gl-pci
      -device "usb-kbd,bus=xhci.0"
      -device "usb-tablet,bus=xhci.0"
      -display "sdl,gl=on"
    )
  fi

  # With -nodefaults, explicitly suppress serial console and monitor.
  QEMU_CMD+=(
    -serial none
    -monitor none
  )
fi

# --- Common optional devices/features ---
if [[ "$ARDUINO" -eq 1 ]]; then
  if ! id -nG | tr ' ' '\n' | grep -qx 'kvm-arduino'; then
    echo "WARNING: current user is not in host group kvm-arduino." >&2
    echo "         Raw Arduino USB passthrough will probably fail." >&2
  fi

  QEMU_CMD+=(
    -device "usb-host,bus=xhci.0,vendorid=0x2341,productid=0x0043"
  )
fi

if [[ -n "$SHARE_DIR" ]]; then
  QEMU_CMD+=(
    -virtfs "local,path=$SHARE_DIR,security_model=none,mount_tag=$SHARE_TAG"
  )
fi

# --- Summary ---
echo "Guest architecture: $ARCH"
echo "Host architecture:  $HOST_ARCH"
echo "Acceleration:       ${ACCEL^^}"
echo "CPU model:          $CPU_MODEL"
echo "vCPUs:              $CORES"
echo "RAM:                ${MEM_MB} MiB"
if [[ "$HEADLESS" -eq 1 ]]; then
  echo "Graphics:           headless"
else
  if [[ "$ARCH" == "amd64" ]]; then
    echo "Graphics:           virtio-vga-gl / SDL OpenGL"
  else
    echo "Graphics:           virtio-gpu-gl-pci / SDL OpenGL"
  fi
fi
echo "Snapshot:           $SNAPSHOT"
if [[ "$SSH_ENABLED" -eq 1 ]]; then
  echo "SSH forwarding:     127.0.0.1:${SSH_PORT} -> guest:22"
else
  echo "SSH forwarding:     off"
fi
echo

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would run:\n  '
  printf '%q ' "${QEMU_CMD[@]}"
  printf '\n'
  exit 0
fi

exec "${QEMU_CMD[@]}"
