#!/usr/bin/env bash
set -euo pipefail

# bunny-images.sh
# Clone golden.qcow2 + golden.OVMF_VARS.fd into bunny00..bunny11
# Convert bunny00..bunny11.qcow2 into bunnyXX.vmdk (monolithicSparse)
# Compress bunny00..bunny11.vmdk into bunnyXX.vmdk.zst
# Create SHA256 checksums for bunny*.vmdk.zst

GOLDEN_QCOW2="golden.qcow2"
GOLDEN_VARS="golden.OVMF_VARS.fd"

usage() {
  cat <<'EOF'
bunny-images.sh — Clone + Convert helper for MCT student VMs

Usage:
  bunny-images.sh --clone
  bunny-images.sh --convert
  bunny-images.sh --zstd
  bunny-images.sh --sha256
  bunny-images.sh --help

Overview (what YOU still need to do manually):
  This script automates only the file operations on the host side:
    1) Clone:   copy golden image -> bunny00..bunny11
    2) MANUAL:  inside each VM run nixos-rebuild for the matching flake output
    3) Convert: qcow2 -> vmdk for bunny00..bunny11
    4) Zstd:    compress vmdk -> vmdk.zst for bunny00..bunny11
    5) SHA256:  create checksums file for bunny*.vmdk.zst (for transfer verification)

Step 1 — Clone (host side, automated by --clone):
  Copies these two files:
    golden.qcow2          -> bunnyXX.qcow2
    golden.OVMF_VARS.fd   -> bunnyXX.OVMF_VARS.fd
  for XX = 00..11.

  Safety behavior:
    - If bunnyXX.qcow2 already exists, it will NOT be overwritten (warning only).
    - If bunnyXX.OVMF_VARS.fd already exists, it will NOT be overwritten (warning only).

Step 2 — IMPORTANT manual step (inside the VM, user action required):
  After you boot a specific VM (example: bunny07), you MUST run inside that VM:

    sudo nixos-rebuild switch --flake .#bunnyXX

  Replace XX with the VM number you are currently working on (00..11).
  Example inside bunny07:
    sudo nixos-rebuild switch --flake .#bunny07

  Notes:
    - This step is intentionally NOT automated by this script.
    - Run it only after the VM is booted and you are logged in.

Step 3 — Convert to VMware format (host side, automated by --convert):
  Converts:
    bunnyXX.qcow2 -> bunnyXX.vmdk
  using the exact command:

    qemu-img convert -p -f qcow2 -O vmdk -o subformat=monolithicSparse \
      bunnyXX.qcow2 bunnyXX.vmdk

  Important behavior:
    - If bunnyXX.vmdk already exists: conversion is SKIPPED and a warning is printed.
    - If bunnyXX.qcow2 is missing: conversion is SKIPPED and a warning is printed.

Step 4 — Compress VMDKs with zstd (host side, automated by --zstd):
  Compresses:
    bunnyXX.vmdk -> bunnyXX.vmdk.zst

  Important behavior:
    - If bunnyXX.vmdk.zst already exists: compression is SKIPPED and a warning is printed.
    - If bunnyXX.vmdk is missing: compression is SKIPPED and a warning is printed.

  Notes:
    - This keeps the original .vmdk (no deletion).
    - Uses zstd with multiple threads (if available).

Step 5 — Create SHA256 checksums (host side, automated by --sha256):
  Writes SHA256 checksums ONLY for:
    bunny*.vmdk.zst
  into:
    checksums.sha256

  Exact Linux command used:
    sha256sum bunny*.vmdk.zst > checksums.sha256

  Manual verification on Windows (example):
    certutil -hashfile bunny00.vmdk.zst SHA256

  Then compare the shown SHA256 value with the matching line in checksums.sha256.

Requirements:
  --clone   : golden.qcow2 and golden.OVMF_VARS.fd must exist in the current directory
  --convert : qemu-img must be available in PATH
  --zstd    : zstd must be available in PATH
  --sha256  : sha256sum must be available in PATH

Examples:
  ./bunny-images.sh --clone
  ./bunny-images.sh --convert
  ./bunny-images.sh --zstd
  ./bunny-images.sh --sha256

EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

warn() {
  echo "WARN:  $*" >&2
}

need_file() {
  local f="$1"
  [[ -f "$f" ]] || die "Missing required file: $f"
}

need_cmd() {
  local c="$1"
  command -v "$c" >/dev/null 2>&1 || die "Missing required command in PATH: $c"
}

for_xx_00_to_11() {
  local xx
  for xx in $(seq -w 0 11); do
    echo "$xx"
  done
}

do_clone() {
  need_file "$GOLDEN_QCOW2"
  need_file "$GOLDEN_VARS"

  for xx in $(for_xx_00_to_11); do
    local dst_qcow2="bunny${xx}.qcow2"
    local dst_vars="bunny${xx}.OVMF_VARS.fd"

    if [[ -e "$dst_qcow2" ]]; then
      warn "Skipping copy: $dst_qcow2 already exists"
    else
      echo "Copying $GOLDEN_QCOW2 -> $dst_qcow2"
      cp --reflink=auto --sparse=always "$GOLDEN_QCOW2" "$dst_qcow2"
    fi

    if [[ -e "$dst_vars" ]]; then
      warn "Skipping copy: $dst_vars already exists"
    else
      echo "Copying $GOLDEN_VARS -> $dst_vars"
      cp --reflink=auto "$GOLDEN_VARS" "$dst_vars"
    fi
  done
}

do_convert() {
  need_cmd "qemu-img"

  for xx in $(for_xx_00_to_11); do
    local src="bunny${xx}.qcow2"
    local dst="bunny${xx}.vmdk"

    if [[ -e "$dst" ]]; then
      warn "Skipping convert: $dst already exists"
      continue
    fi

    if [[ ! -f "$src" ]]; then
      warn "Skipping convert: missing source $src"
      continue
    fi

    echo "Converting $src -> $dst"
    qemu-img convert -p -f qcow2 -O vmdk -o subformat=monolithicSparse "$src" "$dst"
  done
}

do_zstd() {
  need_cmd "zstd"

  for xx in $(for_xx_00_to_11); do
    local src="bunny${xx}.vmdk"
    local dst="${src}.zst"

    if [[ -e "$dst" ]]; then
      warn "Skipping zstd: $dst already exists"
      continue
    fi

    if [[ ! -f "$src" ]]; then
      warn "Skipping zstd: missing source $src"
      continue
    fi

    echo "Compressing $src -> $dst"
    # Keep original .vmdk (no --rm). Use all cores if supported.
    zstd -T0 "$src" -o "$dst"
  done
}

do_sha256() {
  need_cmd "sha256sum"

  # Collect matches safely (avoid literal pattern if none exist)
  shopt -s nullglob
  local files=(bunny*.vmdk.zst)
  shopt -u nullglob

  if [[ ${#files[@]} -eq 0 ]]; then
    warn "No files matched: bunny*.vmdk.zst — not writing checksums.sha256"
    return 0
  fi

  echo "Writing SHA256 checksums for ${#files[@]} file(s) -> checksums.sha256"
  sha256sum "${files[@]}" > checksums.sha256
}

main() {
  if [[ $# -eq 0 ]]; then
    usage
    exit 1
  fi

  case "${1:-}" in
    --help|-h)
      usage
      ;;
    --clone)
      do_clone
      ;;
    --convert)
      do_convert
      ;;
    --zstd)
      do_zstd
      ;;
    --sha256)
      do_sha256
      ;;
    *)
      die "Unknown option: $1 (use --help)"
      ;;
  esac
}

main "$@"
