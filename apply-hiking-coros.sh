#!/usr/bin/env bash
# Parcourt hiking/, applique applyspeed.py a chaque GPX, recopie
# l'arborescence dans hiking-coros/ sous le meme nom de fichier.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
HIKING="${HIKING:-$ROOT/hiking}"
OUT="${OUT:-$ROOT/hiking-coros}"
SYNTH="${SYNTH:-$ROOT/AdelbodenRandonnée20260915100502_synthese.csv}"
PYTHON="${PYTHON:-python3}"
APPLY="$ROOT/applyspeed.py"

if [[ ! -d "$HIKING" ]]; then
  echo "dossier hiking introuvable: $HIKING" >&2
  exit 1
fi
if [[ ! -f "$SYNTH" ]]; then
  echo "synthese introuvable: $SYNTH (lancer avgspeed.py d'abord)" >&2
  exit 1
fi
if [[ ! -f "$APPLY" ]]; then
  echo "applyspeed.py introuvable: $APPLY" >&2
  exit 1
fi

mkdir -p "$OUT"
count=0
fail=0

while IFS= read -r -d '' src; do
  rel="${src#"$HIKING"/}"
  case "$rel" in
    *_timed.gpx|*_timed-*.gpx) continue ;;
  esac
  dest="$OUT/$rel"
  mkdir -p "$(dirname "$dest")"
  echo "==> $rel"
  if ! "$PYTHON" "$APPLY" "$src" --synthesis "$SYNTH" --output "$dest" "$@"; then
    echo "ECHEC $rel" >&2
    fail=$((fail + 1))
    continue
  fi
  count=$((count + 1))
done < <(find "$HIKING" -type f -name '*.gpx' -print0 | sort -z)

echo "termine: $count gpx ecrits dans $OUT ($fail echecs)"
exit $((fail > 0 ? 1 : 0))
