#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
ROOT_NAME=$(basename -- "$ROOT")
REPOS_DIR="$ROOT/repos"
OUTPUT=${1:-"$(dirname -- "$ROOT")/${ROOT_NAME}-chatgpt.zip"}

for cmd in git tar zip; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: required command not found: $cmd" >&2
        exit 1
    }
done

canonical_path() {
    (cd -- "$1" && pwd -P)
}

assert_git_repo_root() {
    local repo=$1
    local expected actual
    expected=$(canonical_path "$repo")
    actual=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || {
        echo "ERROR: not a Git repository: $repo" >&2
        exit 1
    }
    actual=$(canonical_path "$actual")
    if [[ "$actual" != "$expected" ]]; then
        echo "ERROR: directory is not the root of its own Git repository: $repo" >&2
        echo "       Git root is: $actual" >&2
        exit 1
    fi
}

archive_head() {
    local repo=$1
    local target=$2
    mkdir -p -- "$target"
    git -C "$repo" archive --format=tar HEAD | tar -xf - -C "$target"
}

write_git_state() {
    local repo=$1
    local label=$2
    local branch head origin status

    branch=$(git -C "$repo" branch --show-current)
    [[ -n "$branch" ]] || branch="<detached>"
    head=$(git -C "$repo" rev-parse HEAD)
    origin=$(git -C "$repo" remote get-url origin 2>/dev/null || true)
    [[ -n "$origin" ]] || origin="<none>"
    status=$(git -C "$repo" status --short)

    {
        echo "=== $label ==="
        echo "path: $label"
        echo "branch: $branch"
        echo "HEAD: $head"
        echo "origin: $origin"
        echo "status:"
        if [[ -n "$status" ]]; then
            printf '%s\n' "$status"
        else
            echo "  clean"
        fi
        echo
    } >> "$STATE_FILE"
}

assert_git_repo_root "$ROOT"

repos=( )
if [[ -d "$REPOS_DIR" ]]; then
    while IFS= read -r -d '' dir; do
        assert_git_repo_root "$dir"
        repos+=("$dir")
    done < <(find "$REPOS_DIR" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
fi

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
STAGE="$tmp/$ROOT_NAME"
STATE_FILE="$STAGE/GIT-STATE.txt"

# The outer repository deliberately ignores repos/. Export its committed HEAD
# first, then add every direct child of repos/ from that repository's own HEAD.
archive_head "$ROOT" "$STAGE"
: > "$STATE_FILE"
write_git_state "$ROOT" "."

for repo in "${repos[@]}"; do
    name=$(basename -- "$repo")
    archive_head "$repo" "$STAGE/repos/$name"
    write_git_state "$repo" "repos/$name"
done

mkdir -p -- "$(dirname -- "$OUTPUT")"
rm -f -- "$OUTPUT"
(
    cd -- "$tmp"
    zip -qr "$OUTPUT" "$ROOT_NAME"
)

echo "Created: $OUTPUT"
echo "Included committed HEAD from:"
echo "  $ROOT_NAME"
for repo in "${repos[@]}"; do
    echo "  repos/$(basename -- "$repo")"
done

if git -C "$ROOT" status --short | grep -q .; then
    echo "WARNING: $ROOT_NAME has uncommitted/untracked changes; they are NOT in the archive." >&2
fi
for repo in "${repos[@]}"; do
    if git -C "$repo" status --short | grep -q .; then
        echo "WARNING: repos/$(basename -- "$repo") has uncommitted/untracked changes; they are NOT in the archive." >&2
    fi
done
