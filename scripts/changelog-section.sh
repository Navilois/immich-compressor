#!/usr/bin/env bash
# Print one version's section of CHANGELOG.md — the body of a GitHub release.
#
#   ./scripts/changelog-section.sh 1.1.0
#   ./scripts/changelog-section.sh v1.1.0     # a leading v is accepted
#
# Exits non-zero when the version has no section, so a release cannot be published with
# an empty body by accident.
set -euo pipefail

cd "$(dirname "$0")/.."

version="${1:?usage: changelog-section.sh <version>}"
version="${version#v}"

section=$(awk -v want="## [$version]" '
  index($0, want) == 1 { printing = 1; next }
  printing && /^## \[/  { exit }
  printing              { print }
' CHANGELOG.md)

# Strip the blank lines the extraction leaves at both ends.
section=$(printf '%s\n' "$section" | sed -e '/./,$!d' | tac | sed -e '/./,$!d' | tac)

if [ -z "$section" ]; then
  echo "no CHANGELOG.md section for version $version" >&2
  exit 1
fi

printf '%s\n' "$section"
