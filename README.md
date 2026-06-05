# B6-LCA-Agent — KI-gestützte Ökobilanzierung von Gebäude-Clustern (Modul B6)

> **Software-Artefakt einer Bachelorarbeit** · Jade Hochschule Wilhelmshaven/Oldenburg/Elsfleth
> Fachbereich Architektur, Studiengang *Urban Design*, Campus Oldenburg

Dieses Repository enthält das im Rahmen einer Bachelorarbeit entwickelte
**autonome Multi-Agenten-System zur Bilanzierung von Modul B6 (Betriebsenergie)
nach DIN EN 15978**. Es ist die softwaretechnische Umsetzung der in der Arbeit
untersuchten *KI-gestützten Methode* und dient dem reproduzierbaren,
ortsbezogenen Vergleich gegen eine manuelle Referenzbilanz und eine etablierte
Simulationssoftware.

---

## Akademischer Kontext

| | |
|---|---|
| **Titel der Arbeit** | *Effizienz und Validität der Ökobilanzierung von Gebäude-Clustern: Ein methodischer Vergleich zwischen manueller, softwarebasierter (CEA) und KI-gestützter Analyse am Beispiel der Jade Hochschule Oldenburg* |
| **Autor** | Nik Ansre |
| **Studiengang** | Urban Design (B.A.), Fachbereich Architektur, Campus Oldenburg |
| **Hochschule** | Jade Hochschule |
| **Erstprüferin** | Prof. Dr.-Ing. Radostina Radulova-Stahmer |
| **Zweitprüfer** | M.Sc. Tobias Neiß-Theuerkauff |
| **Einreichung** | Sommersemester 2026 |
| **Rolle dieses Repos** | Software-Artefakt der *KI-gestützten Methode* (Multi-Agenten-Pipeline) |

> ⚠️ **Hinweis zur akademischen Integrität:** Dieses Repository ist Bestandteil
> einer Prüfungsleistung. Es wird zu Dokumentations-, Nachvollziehbarkeits- und
> Archivierungszwecken veröffentlicht. Eine Wiederverwendung im Rahmen eigener
> Prüfungsleistungen ohne korrekte Zitation ist nicht zulässig (siehe
> [Zitation](#zitation) und [Lizenz](#lizenz)).

---

## Forschungseinordnung

Die Bachelorarbeit vergleicht **drei methodische Pfade** zur B6-Bilanzierung
desselben realen Gebäude-Clusters (Jade Hochschule, Standort Oldenburg) anhand
der Kriterien **Aufwand, Validität, Reproduzierbarkeit und Skalierbarkeit**:

| Pfad | Methode | Werkzeug | Dieses Repo? |
|---|---|---|:---:|
| A | Manuelle Bilanzierung | Tabellenkalkulation, Ortsbegehung | – |
| B | Softwarebasierte Quartierssimulation | City Energy Analyst (CEA) | – |
| **C** | **KI-gestützte autonome Bilanzierung** | **dieses Multi-Agenten-System** | **✅** |

Alle drei Pfade berechnen **bedarfsbasiert** (nicht verbrauchsbasiert), um
Vergleichbarkeit herzustellen. Die hier implementierte Methode C berechnet den
Energiebedarf auf Basis der **DIN V 18599**, wobei die Eingangsgrößen
**autonom durch das LLM-gestützte Agentensystem beschafft** werden (OpenStreetMap,
protokollierte Web-Recherche, lokale Datensätze). Da Sprachmodell-Ausgaben nicht
bitgenau reproduzierbar sind, ist eine **nachgelagerte Validierungsschicht**
(Plausibilitätsprüfung + Pedigree-Matrix) integraler Bestandteil des Designs.

### Zentrale Befunde der Arbeit (Auszug)

- **Referenz (Methode A, manuell):** 75,47 kg CO₂eq/(m²·a), Unsicherheitsband ±14,93 %.
- **Methode B (CEA):** unterschätzt das Treibhauspotenzial um −24,0 %, da die
  standardisierte Typologie nutzungsspezifische Hochlastzonen (Gewerbeküche,
  Serverraum) nicht abbildet; native Methodenlücke beim Grünraum.
- **Methode C (dieses Tool):** reduziert den Bearbeitungsaufwand von ca. **15 h
  auf ca. 30 min** bei höherer Datenaktualität durch protokollierte
  Web-Recherchen; unterschätzt das Treibhauspotenzial datenseitig um −25,0 %
  (OSM-Lücken → Standard-Hüllenklassen → Validierungsschicht zwingend).
  Schließt die Grünraum-Methodenlücke durch automatisierte semantische
  Bildsegmentierung, unterschätzt den Kohlenstoff-Speicherbestand jedoch um
  −58,0 % infolge von Kronenverschmelzungen in dichten Gehölzstrukturen.

Aus den Stärken-Schwächen-Profilen leitet die Arbeit ein **hybrides
Mensch-KI-Framework** für die Planungspraxis ab.

---

## Methodische Grundlagen

| Aspekt | Umsetzung |
|---|---|
| Bilanzrahmen | **Modul B6** (Betriebsenergie) nach **DIN EN 15978**; A1–A5, B1–B5, C1–C4 = MND |
| Energiebedarf | Quasi-stationäre Monatsbilanz nach **DIN V 18599-2** (vereinfacht, Annex-C-Nutzungsfaktor) |
| Primärenergie | PEF-Kette nach **GEG 2024 Anlage 4** |
| Emissionsfaktoren | **ÖKOBAUDAT 2024-I**-Snapshot, EN 15804+A2 (EF 3.1), UUID-gepinnt, 1-kWh-normiert |
| Klimadaten | TMYx Oldenburg 2011–2025 (Copernicus ERA5, EPW) |
| Datenqualität | **Pedigree-Matrix** nach Weidema & Wesnæs (1996) und Ciroth et al. (2016) — 5 Dimensionen |
| Unsicherheit | Gauß'sche Fortpflanzung relativer Standardabweichungen |
| Grünraum (Spec §8) | Semantische Bildsegmentierung (DEEPNESS-ONNX) + Allometrie CD→DBH→AGB→C→CO₂; getrennt von B6 |

---

## Systemarchitektur

Orchestrator-gesteuertes **Multi-Agenten-Pattern** mit sechs Fachagenten. Eingabe
ist ein auf einer Karte gezeichnetes AOI-Polygon; Ausgabe ist eine
nachvollziehbare B6-Bilanz inklusive Quellen-, Unsicherheits- und
Datenqualitätsdokumentation.

```
AOI-Polygon (Web-UI, Leaflet)
        │
        ▼
1. Geometrie-Agent     OSM via osmnx → EPSG:25832, Containment-Regel
        ▼
2. Vegetations-Agent   DOP20 ODER ESRI-Satellit → DEEPNESS-ONNX-Segmentierung
                       + OSM-Vegetations-Tags (Kreuzvalidierung) + Allometrie
        ▼
3. Research-Agent       OSM-Lückenfüllung; Baujahr per Web-Recherche (RAG,
                       zitierpflichtig); Plausibilität; Pedigree-Matrix
        ▼
4. Material-Agent       ÖKOBAUDAT-Vorrangkette, UUID-gepinnt, EN 15804+A2
        ▼
5. Energie-Agent        DIN V 18599-2 Monatsbilanz, GEG-PEF, Gauß-Unsicherheit
        ▼
6. Reporting-Agent      report.md / PDF / JSON / CSV / manifest.json + KI-Empfehlungen
```

**Web-Recherche-Framework & Anti-Halluzination:** Statt eines modellseitigen
Web-Tools führt das System die Suche selbst aus (schlüsselloses DuckDuckGo) und
übergibt dem LLM nur die *echten* Treffer. Übernommene Werte müssen zwingend eine
der real gelieferten Quell-URLs zitieren — andernfalls werden sie verworfen
(Erfüllung der No-Fabrication-Regel). Dadurch ist jeder recherchierte Wert
quellenbelegt und im Bericht nachvollziehbar.

Technische Kennzahlen: ~5.500 Zeilen Backend-Python (FastAPI, 6 Agenten,
11 Berechnungs-Engines), ~900 Zeilen Frontend-TypeScript (React/Leaflet/Recharts),
93 automatisierte Tests.

---

## Installation und Ausführung

### Voraussetzungen

- **Docker Desktop** muss installiert sein und laufen — es ist die *einzige*
  zwingende Software-Voraussetzung (enthält Docker Engine + Compose).
  Download: <https://www.docker.com/products/docker-desktop/>
  Ein lokales Python oder Node.js ist **nicht** erforderlich — alles läuft im Container.
- **OpenRouter-API-Key** für die LLM-Aufrufe.
  Erstellen: <https://openrouter.ai/settings/keys>

### Variante A — geführte Skripte (empfohlen)

| # | Schritt | Windows | Linux / macOS |
|--:|---|---|---|
| 1 | API-Key konfigurieren | `config.bat` | `./config.sh` |
| 2 | Tool starten | `start.bat`  | `./start.bash`  |
| 3 | Browser öffnen: <http://localhost:5173> | | |

`config` fragt den API-Key einmalig ab (Eingabe wird nicht angezeigt, nie
geloggt, nie ins Container-Image gebacken). `start` prüft Docker, baut die
Container und führt einen Health-Check durch.

### Variante B — manuell (ohne Skripte, alle Betriebssysteme)

```bash
# 1) Konfigurationsdatei aus der Vorlage anlegen
cp .env.example .env
#    danach OPENROUTER_API_KEY=sk-or-... in der Datei .env eintragen

# 2) Container bauen und starten (erster Build dauert einige Minuten)
docker compose up --build

# 3) Im Browser öffnen:
#    UI:          http://localhost:5173
#    API-Health:  http://localhost:8000/api/health
```

### Stoppen / Logs / Neustart

```bash
docker compose down        # Container stoppen
docker compose logs -f     # Live-Logs ansehen
docker compose up -d       # im Hintergrund (detached) starten
```

> Beim allerersten Start lädt Docker die Basis-Images und installiert alle
> Abhängigkeiten — das dauert je nach Internetverbindung mehrere Minuten.
> Danach startet der Container in Sekunden.

---

## Reproduzierbarkeit

Da die Methode stochastische LLM-Komponenten enthält, ist Reproduzierbarkeit ein
explizites Designziel:

- **Gepinnte Abhängigkeiten** (`backend/requirements.txt`, `package-lock.json`).
- **Versionierte Datensätze** (ÖKOBAUDAT-Snapshot mit Datum, TMYx-Klimajahr,
  Profil-YAMLs).
- **Per-Lauf-Manifest** (`runs/<id>/results/manifest.json`): Image-Tag,
  Python-Version, Seed (42), Bezugsjahr, ÖKOBAUDAT-Version, Input-Hashes,
  kumulative LLM-Nutzung (Tokens, Web-Suchen, Kosten).
- **Protokollierte Web-Recherchen** mit zitierten Quell-URLs je übernommenem Wert.
- **Containerisierung** garantiert identische Laufzeitumgebung auf jedem Gerät.

---

## Verzeichnisstruktur (gekürzt)

```
.
├── config.{bat,sh}     API-Key sicher in .env eintragen
├── start.{bat,bash}    Container bauen + starten
├── docker-compose.yml  Backend + Frontend
├── models.yaml         Per-Agent-LLM-Routing
├── backend/app/
│   ├── orchestrator.py          Pipeline-Steuerung
│   ├── agents/                  6 Fachagenten
│   ├── engine/                  Deterministische Berechnungs-Engines
│   │   ├── balance.py           DIN V 18599-2 Monatsbilanz
│   │   ├── vegetation.py        ONNX-Segmentierung + Allometrie
│   │   ├── osm_vegetation.py    OSM-Vegetations-Tags
│   │   ├── satellite_tiles.py   ESRI-Satellit-Fallback
│   │   └── pedigree.py          Weidema-DQI
│   ├── llm/                     OpenRouter-Client + Web-Recherche (RAG)
│   └── validation.py            Plausibilitätsschicht
├── frontend/           React + Vite + Leaflet
├── data/               ÖKOBAUDAT, DEEPNESS-Modell, Klima, Profile (read-only)
└── runs/               Pro-Lauf-Artefakte (Bericht, Logs, Quellen, Manifest)
```

Eine ausführliche technische Beschreibung der einzelnen Agenten, Engines und
Konfigurationsschalter befindet sich in den Code-Docstrings sowie in
[`.env.example`](.env.example).

---

## Datenquellen und Lizenzen

| Datenquelle | Herkunft | Lizenz |
|---|---|---|
| Gebäudegeometrie | OpenStreetMap (osmnx/Overpass) | ODbL |
| Emissionsfaktoren | ÖKOBAUDAT 2024-I (BBSR/BBR) | dl-de/by-2-0 |
| Klimadaten | TMYx Oldenburg (climate.onebuilding.org, ERA5) | frei nutzbar |
| Vegetationsmodell | DEEPNESS LandCover.ai (ONNX) | siehe `data/deepness/` |
| Hochaufl. Luftbild | LGLN DOP20 (optional, Niedersachsen) | dl-de/by-2-0 |
| Satellit-Fallback | ESRI World Imagery (XYZ) | ESRI-Nutzungsbedingungen |
| Web-Recherche | DuckDuckGo (`ddgs`, schlüssellos) | – |

---

## Bekannte Grenzen (Diskussionsteil der Arbeit)

- **Datenseitige Unterschätzung** des Treibhauspotenzials (−25,0 %), da OSM-Lücken
  über Standard-Hüllenklassen geschlossen werden → Validierungsschicht zwingend.
- **Grünraum-Unterschätzung** (−58,0 %) durch Kronenverschmelzungen in dichten
  Gehölzstrukturen bei der semantischen Segmentierung.
- **Geografische Relevanz der Web-Treffer** wird nicht inhaltlich geprüft (nur
  URL-Herkunft) → seltene Fehlzuordnungen möglich.
- **Vereinfachungen** gegenüber dem 10-teiligen DIN-V-18599-Vollausbau (eine Zone
  pro Gebäude, gemittelte Anlagentechnik, profilbasierter Strombedarf, keine
  tageslichtgestützte Beleuchtungssimulation).
- **Stochastik** der LLM-Komponenten — daher Pedigree-Matrix und protokollierte
  Quellen als Transparenzinstrument.

---

## Zitation

Bei Bezugnahme auf dieses Software-Artefakt:

```bibtex
@thesis{Ansre2026B6LCAAgent,
  author      = {Ansre, Nik},
  title       = {Effizienz und Validit{\"a}t der {\"O}kobilanzierung von
                 Geb{\"a}ude-Clustern: Ein methodischer Vergleich zwischen
                 manueller, softwarebasierter (CEA) und KI-gest{\"u}tzter
                 Analyse am Beispiel der Jade Hochschule Oldenburg},
  type        = {Bachelorarbeit},
  institution = {Jade Hochschule, Fachbereich Architektur, Campus Oldenburg},
  year        = {2026},
  note        = {Software-Artefakt: B6-LCA-Agent (Multi-Agenten-System)}
}
```

---

## Lizenz

Der **Quellcode** dieses Repositorys steht – sofern nicht anders angegeben – unter
der MIT-Lizenz zur Verfügung (siehe `LICENSE`, falls vorhanden).

Die **eingebundenen Datensätze** unterliegen den jeweils in der Tabelle
[Datenquellen und Lizenzen](#datenquellen-und-lizenzen) genannten Bedingungen.

Die **inhaltliche Arbeit** (Bachelorarbeit, Abbildungen, Ergebnisse) ist
urheberrechtlich geschützt; eine Nutzung ist nur unter korrekter Quellenangabe
zulässig.

---

*Dieses Tool ist ein Forschungsprototyp im Rahmen einer Bachelorarbeit und
ersetzt keine ingenieurmäßige Detailplanung oder zertifizierte LCA-Software.*
