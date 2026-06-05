#!/usr/bin/env bash
###############################################################################
# B6-Bilanzierung (DIN EN 15978) — Start-Skript für Linux / macOS
#
# Baut die Docker-Container und startet das Tool. Reproduzierbar auf jedem
# Gerät mit Docker. Der OPENROUTER_API_KEY wird ausschliesslich aus der
# lokalen .env gelesen und NIEMALS ausgegeben, geloggt oder ins Image gebacken.
#
# Aufruf:   ./start.bash
# Stoppen:  docker compose down
###############################################################################
set -euo pipefail

# Ins Verzeichnis dieses Skripts wechseln (egal von wo aufgerufen).
cd "$(dirname "$0")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say()  { printf "${GREEN}==>${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[!]${NC} %s\n" "$1"; }
die()  { printf "${RED}[FEHLER]${NC} %s\n" "$1" >&2; exit 1; }

echo "=============================================================="
echo " Cluster-Bilanzierung B6 — Betriebsenergie (DIN EN 15978)"
echo "=============================================================="

# --- 1) Docker vorhanden + Daemon laeuft? ------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker ist nicht installiert. Bitte Docker Desktop installieren: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || die "Docker-Daemon laeuft nicht. Bitte Docker Desktop starten und erneut versuchen."

# docker compose v2 (Plugin) bevorzugt, sonst docker-compose v1.
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "Docker Compose nicht gefunden. Bitte ein aktuelles Docker Desktop installieren."
fi
say "Docker gefunden — Compose-Befehl: ${COMPOSE}"

# --- 2) .env vorhanden? Sonst aus Vorlage anlegen ----------------------------
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    warn ".env wurde aus .env.example erstellt."
    warn "Bitte OPENROUTER_API_KEY in der Datei .env eintragen und Skript erneut starten."
    exit 1
  else
    die ".env und .env.example fehlen beide. Repository unvollstaendig?"
  fi
fi

# --- 3) API-Key gesetzt? (Wert wird NIE ausgegeben) --------------------------
# grep -q prueft nur auf Existenz eines nicht-leeren Wertes hinter dem '='.
if ! grep -Eq '^OPENROUTER_API_KEY=.+' .env; then
  die "OPENROUTER_API_KEY ist in .env leer. Bitte den Schluessel eintragen (Zeile: OPENROUTER_API_KEY=sk-or-...)."
fi
say "OPENROUTER_API_KEY ist gesetzt."

# --- 4) Kritische Datensaetze vorhanden? -------------------------------------
missing=0
check_file() {
  if [ ! -f "$1" ]; then warn "fehlt: $1  ($2)"; missing=1; fi
}
check_file "data/deepness/tree_segmentation.onnx"                       "DEEPNESS-Vegetationsmodell"
check_file "data/imagery/AOI.tif"                                       "Luftbild DOP20"
check_file "data/oekobaudat/oekobaudat_2024-I_2026-05-25.csv"           "OEKOBAUDAT-Snapshot"
check_file "data/climate/DEU_NI_Oldenburg.102150_TMYx.2011-2025.epw"    "Klimadaten TMYx"
if [ "$missing" -eq 1 ]; then
  warn "Mindestens ein Datensatz fehlt — Laeufe koennen fehlschlagen. Trotzdem fortfahren? [j/N]"
  read -r ans
  case "$ans" in [jJyY]*) ;; *) die "Abgebrochen. Bitte fehlende Dateien bereitstellen." ;; esac
else
  say "Alle kritischen Datensaetze vorhanden."
fi

# --- 5) Bauen + starten ------------------------------------------------------
say "Baue Container und starte (das kann beim ersten Mal mehrere Minuten dauern)..."
$COMPOSE up -d --build

# --- 6) Auf Backend-Health warten --------------------------------------------
say "Warte auf das Backend (http://localhost:8000/api/health)..."
healthy=0
for _ in $(seq 1 90); do
  if curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; then healthy=1; break; fi
  sleep 2
done
if [ "$healthy" -eq 1 ]; then
  say "Backend ist bereit."
else
  warn "Backend antwortet noch nicht. Logs pruefen mit:  ${COMPOSE} logs -f backend"
fi

# --- 7) Browser oeffnen (best effort) ----------------------------------------
URL="http://localhost:5173"
echo "=============================================================="
say "Web-Oberflaeche:  ${URL}"
say "API-Health:       http://localhost:8000/api/health"
echo
echo " Stoppen:        ${COMPOSE} down"
echo " Logs ansehen:   ${COMPOSE} logs -f"
echo "=============================================================="

if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
fi
