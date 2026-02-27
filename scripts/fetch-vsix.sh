#!/usr/bin/env bash
set -euo pipefail

# Downloads VSIX files into ../assets/vsix/ for offline installation.
# Usage:
#   ./scripts/fetch-vsix.sh <continue_version> <arduino_maker_workshop_version>
# Example:
#   ./scripts/fetch-vsix.sh 0.9.58 1.2.3

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <Continue.continue version> <TheLastOutpostWorkshop.arduino-maker-workshop version>" >&2
  exit 2
fi

CONTINUE_VER="$1"
AMW_VER="$2"

outdir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../assets/vsix" && pwd)"
mkdir -p "$outdir"

continue_url="https://marketplace.visualstudio.com/_apis/public/gallery/publishers/Continue/vsextensions/continue/${CONTINUE_VER}/vspackage"
amw_url="https://marketplace.visualstudio.com/_apis/public/gallery/publishers/TheLastOutpostWorkshop/vsextensions/arduino-maker-workshop/${AMW_VER}/vspackage"

curl -L "$continue_url" -o "$outdir/Continue.continue.vsix"
curl -L "$amw_url" -o "$outdir/TheLastOutpostWorkshop.arduino-maker-workshop.vsix"

echo "OK: downloaded to $outdir" >&2
