#!/usr/bin/env bash
# English-only guard for tracked text files.
#
# The project started as a personal deployment with German comments and commit messages.
# Everything user-visible is English now, and this keeps it that way: it greps tracked
# text for German stopwords that are unlikely to appear in English prose or in code.
#
#   ./scripts/check-language.sh          check the whole tree
#   ./scripts/check-language.sh path...  check specific files
#
# Exits non-zero and prints file:line:match for every hit. Past commit *messages* are
# history and are deliberately not checked.
set -euo pipefail

cd "$(dirname "$0")/.."

# Word-boundary matched, case-insensitive. Chosen so they cannot collide with English or
# with identifiers: no "die"/"war"/"bin"/"man"/"was", which are all legitimate here.
STOPWORDS=(
  aber auch beim damit dann dass denn deren derselbe dieser diese dieses doch durch
  eine einen einer eines etwa fuer für gegen gibt haben hier ihre jedoch kann keine
  koennen können lassen mehr muss müssen nach nicht noch nur oder ohne schon sein
  sich soll sollte sowie ueber über und unter viele vom von wenn werden wird wurde
  wurden zwar zwei zwischen
  # Nouns and phrases from this project's own German history.
  Abschnitt Aenderung Änderung Anleitung Ausgabe Beispiel Berechtigung Datei Einstellung
  Fehler Hinweis Konfiguration Loeschen Löschen Ordner Papierkorb Pruefung Prüfung
  Reihenfolge Schluessel Schlüssel Stufe Ueberpruefung Überprüfung Umgebung Verwendung
  Verzeichnis Vorgang Warnung Zeitpunkt Zugriff
)

pattern=$(printf '|%s' "${STOPWORDS[@]}")
pattern="\\b(${pattern:1})\\b"

if [ "$#" -gt 0 ]; then
  files=("$@")
else
  # Tracked text files only. Binary fixtures, images and the vendored captures are noise.
  mapfile -t files < <(git ls-files -- \
    '*.py' '*.md' '*.yaml' '*.yml' '*.toml' '*.sh' '*.cfg' '*.ini' '*.json' \
    'Dockerfile' 'Makefile' '.editorconfig' '.gitattributes' '.gitignore' \
    ':!:tests/fixtures/**' ':!:docs/config.schema.json')
fi

if [ "${#files[@]}" -eq 0 ]; then
  echo "check-language: nothing to check"
  exit 0
fi

# Exclude this file: it necessarily contains the very words it looks for.
filtered=()
for file in "${files[@]}"; do
  [ "$file" = "scripts/check-language.sh" ] && continue
  [ -f "$file" ] || continue
  filtered+=("$file")
done

if [ "${#filtered[@]}" -eq 0 ]; then
  echo "check-language: nothing to check"
  exit 0
fi

if hits=$(grep -nEIiH "$pattern" "${filtered[@]}"); then
  echo "check-language: German text found in tracked files — this project is English-only." >&2
  echo "$hits" >&2
  exit 1
fi

echo "check-language: ${#filtered[@]} file(s) checked, all English"
