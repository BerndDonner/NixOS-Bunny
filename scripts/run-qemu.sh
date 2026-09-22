#!/usr/bin/env bash
set -euo pipefail

DEFAULT_MEM_MB=4096
DEFAULT_CORES=4
DEFAULT_SSH_PORT=2222
SHARE_TAG="hostshare"

usage() {
  cat <<'EOF'
Usage:
  run-qemu.sh [OPTIONS] <disk.qcow2>

Examples:
  ./run-qemu.sh golden.qcow2
  ./run-qemu.sh --arduino golden.qcow2
  ./run-qemu.sh --ssh golden.qcow2
  ./run-qemu.sh --ssh --port 2223 golden.qcow2
  ./run-qemu.sh --share /home/bernd/VirtualMashines/exam golden.qcow2
  ./run-qemu.sh --arduino --ssh --share ~/exam golden.qcow2

Feature help:
  ./run-qemu.sh --arduino --help
  ./run-qemu.sh --ssh --help
  ./run-qemu.sh --share --help

Options:
  -m, --mem <MB>        RAM in MiB (default: 4096)
  -c, --cores <N>       CPU cores (default: 4)
  --vars <path>         Path to OVMF VARS file
                        (default: <disk>.OVMF_VARS.fd)

  --arduino             Pass the real Arduino Uno R3 USB device through to the VM
  --ssh                 Forward host localhost:2222 to guest SSH port 22
  --port <PORT>         Host port for --ssh (default: 2222)
                        Valid only together with --ssh
  --share <DIRECTORY>   Share one host directory with the VM via virtio-9p
  --headless            Run without an SDL window (for automated provisioning)
  --discard             Pass guest discard/TRIM through to the qcow2 image

  --no-kvm              Disable KVM and use QEMU software emulation
  --dry-run             Print the resulting QEMU command and exit
  -h, --help            Show help

Environment overrides:
  DISK_QCOW2            Default disk image if no positional image is given
  VARS_FD               Default OVMF VARS file
  MEM_MB                Default RAM in MiB
  CORES                 Default CPU cores
  SSH_PORT              Default host SSH port

EOF
}

help_arduino() {
  cat <<'EOF'
Arduino USB passthrough
=======================

Option:
  --arduino

Passes the real Arduino Uno R3 USB device (2341:0043) from the KVM host
directly through to the VM.

Start:
    ./run-qemu.sh --arduino golden.qcow2

QEMU uses:
    -device usb-host,bus=xhci.0,vendorid=0x2341,productid=0x0043

The Arduino does NOT need to be connected when QEMU starts. With
vendor/product matching, QEMU waits for a matching host USB device and
passes it to the guest when it appears.

That means this is valid:
    1. start the VM with --arduino
    2. plug in the Uno later
    3. unplug/replug it while the VM is running

Inside Bunny the real Arduino should normally appear as:
    /dev/ttyACM0

Host-side permissions
---------------------
Raw USB passthrough uses the host's raw USB node, for example:

    /dev/bus/usb/003/019

On the KVM host the udev rule assigns an official Uno R3 (2341:0043) to
the host-only group:

    kvm-arduino

with mode 0660. The user running QEMU must therefore be a member of
kvm-arduino.

This group is HOST-only. Bunny does not need it. Inside Bunny, access to
/dev/ttyACM0 remains controlled normally by the guest's dialout group.

Why real USB passthrough
------------------------
A serial-bridge experiment using the host's /dev/ttyACM0 and QEMU
usb-serial was rejected because:

  - the guest saw an emulated serial adapter rather than the real Uno;
  - it appeared as /dev/ttyUSB* instead of /dev/ttyACM*;
  - unplug/replug could leave the serial backend disconnected;
  - the Arduino's real USB identity/descriptors were hidden.

Useful host checks:
    id -nG | tr ' ' '
' | grep '^kvm-arduino$'
    lsusb -d 2341:0043

Useful guest checks:
    ls -l /dev/ttyACM*
    # if usbutils is installed:
    lsusb -d 2341:0043

Notes:
  - This option currently matches the official Uno R3 USB ID 2341:0043.
  - Arduino clones with another USB ID need an additional host udev rule
    and a matching QEMU rule.
  - If several 2341:0043 devices are connected, vendor/product matching
    cannot distinguish which one you intend to use.
  - QEMU documents an old/recurrent host-USB quirk where, after restarting
    QEMU, unplugging and replugging the USB device may occasionally be
    necessary.

EOF
}

help_ssh() {
  cat <<EOF
SSH port forwarding
===================

Option:
  --ssh

QEMU user networking puts the VM behind NAT. --ssh forwards a TCP
port on the host to TCP port 22 inside the VM.

Default mapping:
    host 127.0.0.1:${DEFAULT_SSH_PORT} -> guest port 22

Start:
    ./run-qemu.sh --ssh golden.qcow2

Connect from another terminal on the HOST:
    ssh -p ${DEFAULT_SSH_PORT} student@localhost

Use another host port:
    ./run-qemu.sh --ssh --port 2223 golden.qcow2

Then connect with:
    ssh -p 2223 student@localhost

Important:
  - --ssh only creates the QEMU port forwarding.
  - An SSH server must be installed and running inside the VM.
  - --port is valid only together with --ssh.
  - The forwarded port is bound to 127.0.0.1, so it is not exposed
    to other machines on the host network.

EOF
}

help_share() {
  cat <<EOF
Host directory sharing
======================

Option:
  --share <DIRECTORY>

Shares one directory from the HOST with the VM using virtio-9p.

Start, for example:
    ./run-qemu.sh --share /home/bernd/VirtualMashines/exam golden.qcow2

The directory is exposed to the VM with the mount tag:
    ${SHARE_TAG}

It is NOT mounted automatically inside the VM.

Inside the VM:
    sudo mkdir -p /mnt/${SHARE_TAG}
    sudo mount -t 9p \\
        -o trans=virtio,version=9p2000.L \\
        ${SHARE_TAG} /mnt/${SHARE_TAG}

Then:
    ls /mnt/${SHARE_TAG}

Unmount inside the VM:
    sudo umount /mnt/${SHARE_TAG}

Important:
  - The files are accessed directly from the host directory.
  - They are not copied into the QCOW2 image.
  - Changes made in /mnt/${SHARE_TAG} therefore change the host files.
  - Currently exactly one --share directory is supported.

EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}


# --- Defaults (overridable via env) ---
DISK_QCOW2="${DISK_QCOW2:-}"
VARS_FD="${VARS_FD:-}"
MEM_MB="${MEM_MB:-$DEFAULT_MEM_MB}"
CORES="${CORES:-$DEFAULT_CORES}"
SSH_PORT="${SSH_PORT:-$DEFAULT_SSH_PORT}"

ARDUINO=0
SSH_ENABLED=0
SHARE_DIR=""
HEADLESS=0
DISCARD=0
NO_KVM=0
DRY_RUN=0
HELP_REQUESTED=0
HELP_ARDUINO=0
HELP_SSH=0
HELP_SHARE=0
PORT_WAS_SET=0

POSITIONAL=()

# --- Parse args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
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
      [[ $# -ge 2 ]] || die "$1 requires a value"
      VARS_FD="$2"
      shift 2
      ;;

    --arduino)
      ARDUINO=1
      HELP_ARDUINO=1
      shift
      ;;

    --ssh)
      SSH_ENABLED=1
      HELP_SSH=1
      shift
      ;;

    --port)
      [[ $# -ge 2 ]] || die "--port requires a value"
      SSH_PORT="$2"
      PORT_WAS_SET=1
      shift 2
      ;;

    --share)
      # Support the requested help syntax:
      #   run-qemu.sh --share --help
      if [[ $# -ge 2 && "$2" != "--help" && "$2" != "-h" ]]; then
        SHARE_DIR="$2"
        shift 2
      else
        shift
      fi
      HELP_SHARE=1
      ;;

    --headless)
      HEADLESS=1
      shift
      ;;

    --discard)
      DISCARD=1
      shift
      ;;

    --no-kvm)
      NO_KVM=1
      shift
      ;;

    --dry-run)
      DRY_RUN=1
      shift
      ;;

    -h|--help)
      HELP_REQUESTED=1
      shift
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

# --- Context-sensitive help ---
if [[ "$HELP_REQUESTED" -eq 1 ]]; then
  feature_help=0

  if [[ "$HELP_ARDUINO" -eq 1 ]]; then
    help_arduino
    feature_help=1
  fi

  if [[ "$HELP_SSH" -eq 1 ]]; then
    help_ssh
    feature_help=1
  fi

  if [[ "$HELP_SHARE" -eq 1 ]]; then
    help_share
    feature_help=1
  fi

  if [[ "$feature_help" -eq 0 ]]; then
    usage
  fi

  exit 0
fi

# --- Validate combinations and values ---
is_positive_integer "$MEM_MB" || die "--mem must be a positive integer"
is_positive_integer "$CORES" || die "--cores must be a positive integer"

if ! is_positive_integer "$SSH_PORT" || (( SSH_PORT > 65535 )); then
  die "--port must be an integer between 1 and 65535"
fi

if [[ "$PORT_WAS_SET" -eq 1 && "$SSH_ENABLED" -eq 0 ]]; then
  die "--port is valid only together with --ssh"
fi

if [[ "$HELP_SHARE" -eq 1 && -z "$SHARE_DIR" ]]; then
  die "--share requires a directory"
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

if [[ -z "$DISK_QCOW2" ]]; then
  echo "ERROR: Missing disk image argument." >&2
  echo >&2
  usage >&2
  exit 2
fi

# Derive VARS path from final disk path unless explicitly set.
if [[ -z "$VARS_FD" ]]; then
  VARS_FD="${DISK_QCOW2%.*}.OVMF_VARS.fd"
fi

# --- Resolve OVMF from Nixpkgs ---
OVMF_FV="$(nix eval --raw nixpkgs#OVMF.fd.outPath)/FV"
OVMF_CODE="$OVMF_FV/OVMF_CODE.fd"
OVMF_VARS_TEMPLATE="$OVMF_FV/OVMF_VARS.fd"

[[ -r "$DISK_QCOW2" ]] || {
  echo "ERROR: Disk image not found/readable: $DISK_QCOW2" >&2
  exit 1
}

[[ -r "$OVMF_CODE" ]] || {
  echo "ERROR: OVMF_CODE.fd not found: $OVMF_CODE" >&2
  exit 1
}

if [[ -e "$VARS_FD" && ! -w "$VARS_FD" ]]; then
  echo "ERROR: VARS file exists but is not writable: $VARS_FD" >&2
  echo "Hint: try 'sudo chown $USER:$USER \"$VARS_FD\"' or choose --vars <path>" >&2
  exit 1
fi

# Create writable persistent UEFI NVRAM on first use.
if [[ ! -e "$VARS_FD" ]]; then
  echo "Creating VARS file: $VARS_FD"
  cp "$OVMF_VARS_TEMPLATE" "$VARS_FD"
  chmod u+w "$VARS_FD"
fi

# --- Build QEMU command ---
if [[ "$NO_KVM" -eq 1 ]]; then
  ACCEL_ARGS=(-accel tcg -cpu max)
else
  ACCEL_ARGS=(-accel kvm -cpu host)
fi

NETDEV="user,id=n1"
if [[ "$SSH_ENABLED" -eq 1 ]]; then
  NETDEV+=",hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22"
fi

DISK_DRIVE="file=$DISK_QCOW2,if=virtio,format=qcow2"
if [[ "$DISCARD" -eq 1 ]]; then
  DISK_DRIVE+=",discard=unmap,detect-zeroes=unmap"
fi

QEMU_CMD=(
  qemu-system-x86_64
  "${ACCEL_ARGS[@]}"
  -m "$MEM_MB"
  -smp "cores=$CORES,threads=1,sockets=1"
  -machine q35
  -boot order=c
  -device qemu-xhci,id=xhci
  -device usb-tablet,bus=xhci.0
)

if [[ "$HEADLESS" -eq 1 ]]; then
  QEMU_CMD+=(
    -device virtio-vga
    -display none
  )
else
  QEMU_CMD+=(
    -device virtio-vga-gl
    -display sdl,gl=on
  )
fi

QEMU_CMD+=(
  -netdev "$NETDEV"
  -device virtio-net-pci,netdev=n1
  -drive "$DISK_DRIVE"
  -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE"
  -drive "if=pflash,format=raw,file=$VARS_FD"
)

if [[ "$ARDUINO" -eq 1 ]]; then
  # Deliberately do not require the board to be connected at VM startup.
  # usb-host with vendor/product matching can attach the matching device
  # when it appears later on the host.
  if ! id -nG | tr ' ' '
' | grep -qx 'kvm-arduino'; then
    echo "WARNING: --arduino: current user is not in host group kvm-arduino." >&2
    echo "         Raw USB passthrough will probably fail when the Uno appears." >&2
    echo "         After adding the group via NixOS, log out and back in once." >&2
  fi

  QEMU_CMD+=(
    -device usb-host,bus=xhci.0,vendorid=0x2341,productid=0x0043
  )
fi

if [[ -n "$SHARE_DIR" ]]; then
  QEMU_CMD+=(
    -virtfs "local,path=$SHARE_DIR,security_model=none,mount_tag=$SHARE_TAG"
  )
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'Would run:\n'
  printf '  %q' "${QEMU_CMD[@]}"
  printf '\n'
  exit 0
fi

exec "${QEMU_CMD[@]}"
