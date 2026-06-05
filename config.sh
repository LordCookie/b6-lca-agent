#!/usr/bin/env bash
###############################################################################
# B6-Bilanzierung (DIN EN 15978) — Konfigurations-Skript für Linux / macOS
#
# Trägt den OpenRouter-API-Key SICHER in die lokale .env ein.
# - Eingabe wird NICHT angezeigt (Bash `read -s`).
# - Schlüssel wird NIE ausgegeben, NIE geloggt, NIE in Prozess-Argumente kopiert.
# - .env-Datei bekommt 0600-Rechte (nur Eigentümer lesbar).
#
# Aufruf:   ./config.sh
# Danach:   ./start.bash
###############################################################################
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say()  { printf "${GREEN}==>${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[!]${NC} %s\n" "$1"; }
die()  { printf "${RED}[FEHLER]${NC} %s\n" "$1" >&2; exit 1; }

echo "=============================================================="
echo " B6-Bilanzierung — Konfiguration"
echo "=============================================================="

# --- 1) Sicherstellen, dass .env existiert -----------------------------------
if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    die ".env.example fehlt. Repository unvollständig?"
  fi
  cp .env.example .env
  say ".env aus .env.example erstellt."
else
  say ".env existiert — bestehender Schlüssel wird überschrieben."
fi

# --- 2) Key abfragen (Eingabe wird nicht angezeigt) --------------------------
echo
echo "OpenRouter-API-Key eingeben (die Eingabe wird NICHT angezeigt)."
echo "  Holen unter: https://openrouter.ai/settings/keys"
echo "  Format: sk-or-v1-…"
echo
printf "OPENROUTER_API_KEY: "
# read -s: Eingabe verstecken; -r: Backslash literal
IFS= read -rs API_KEY
echo  # Zeilenumbruch nach versteckter Eingabe

if [ -z "${API_KEY:-}" ]; then
  die "Leerer Schlüssel — abgebrochen."
fi

case "$API_KEY" in
  sk-or-*) ;;
  *)
    warn "Schlüssel beginnt nicht mit 'sk-or-'. Sicher, dass es ein OpenRouter-Key ist?"
    printf "Trotzdem speichern? [j/N] "
    read -r ans
    case "$ans" in
      [jJyY]*) ;;
      *) die "Abgebrochen."  ;;
    esac
    ;;
esac

# --- 3) .env aktualisieren ohne den Wert anderswo zu leaken ------------------
# Pure-Shell Zeilenfilter: kein awk -v / sed-Argument (Prozess-Argv-Leak vermeiden).
tmpfile="$(mktemp)"
trap 'rm -f "$tmpfile"' EXIT

found=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    OPENROUTER_API_KEY=*)
      printf 'OPENROUTER_API_KEY=%s\n' "$API_KEY" >> "$tmpfile"
      found=1
      ;;
    *)
      printf '%s\n' "$line" >> "$tmpfile"
      ;;
  esac
done < .env

if [ "$found" -eq 0 ]; then
  printf 'OPENROUTER_API_KEY=%s\n' "$API_KEY" >> "$tmpfile"
fi

mv "$tmpfile" .env
chmod 600 .env 2>/dev/null || true

# API_KEY-Variable aus dem Prozess-Speicher entfernen
unset API_KEY

say "OPENROUTER_API_KEY in .env gesetzt (Rechte 0600)."
echo
echo "Weitere Einstellungen (REFERENCE_YEAR, WEB_SEARCH_*, VEGETATION_*, …)"
echo "kannst du direkt in der Datei .env editieren — alle haben sinnvolle Defaults."
echo
echo "Nächster Schritt:"
echo "  ./start.bash      # Tool bauen und starten"
echo
