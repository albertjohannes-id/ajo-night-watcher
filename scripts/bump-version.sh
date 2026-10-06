#!/bin/bash
# bump-version.sh — bump the MAJOR.MINOR.PATCH version in VERSION.
#
# Usage:
#   ./scripts/bump-version.sh [patch|minor|major]   (default: patch)
#
# patch: 1.0.0 -> 1.0.1   minor: 1.0.1 -> 1.1.0   major: 1.1.0 -> 2.0.0
set -euo pipefail
cd "$(dirname "$0")/.."

PART="${1:-patch}"
case "$PART" in
  patch|minor|major) ;;
  -h|--help) echo "Usage: bump-version.sh [patch|minor|major]"; exit 0 ;;
  *) echo "Unknown part: $PART (expected patch, minor or major)." >&2; exit 2 ;;
esac

CURRENT="$(tr -d ' \t\r\n' < VERSION)"
if [[ ! "$CURRENT" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "Invalid VERSION: $CURRENT (expected MAJOR.MINOR.PATCH like 1.0.0)." >&2
  exit 1
fi
MAJOR="${BASH_REMATCH[1]}"; MINOR="${BASH_REMATCH[2]}"; PATCH="${BASH_REMATCH[3]}"

case "$PART" in
  patch) PATCH=$(( PATCH + 1 )) ;;
  minor) MINOR=$(( MINOR + 1 )); PATCH=0 ;;
  major) MAJOR=$(( MAJOR + 1 )); MINOR=0; PATCH=0 ;;
esac

echo "$MAJOR.$MINOR.$PATCH" > VERSION
printf 'Bumped %s -> %s\n' "$CURRENT" "$MAJOR.$MINOR.$PATCH"
