# DEEPNESS ONNX-Modell

Lege hier `tree_segmentation.onnx` ab (Spec §2, §8). Lokale Inferenz über
`onnxruntime`, headless — kein QGIS-GUI, keine Cloud. Volume-Mount: `data/`
ist im Container unter `/app/data` read-only verfügbar.

## Modellauswahl aus dem DEEPNESS Model Zoo

Stand 2026-05-25 enthält der [DEEPNESS Model Zoo](https://qgis-plugin-deepness.readthedocs.io/en/latest/main/main_model_zoo.html)
**kein dediziertes Kronen-Segmentation-Modell**. Zwei realistische Optionen:

### Option A — Land Cover Segmentation (empfohlen)

- **Download:** <https://chmura.put.poznan.pl/s/PnAFJw27uneROkV>
- **Task:** Semantic Segmentation, 6 Klassen (LandCover.ai-Datensatz, Polen)
- **Eingangsauflösung:** 25–50 cm/px → passt nativ zu DOP20 (Niedersachsen)
- **Tile-Größe:** 512×512
- **Relevante Klasse:** `2 = Woodland`
- **Config:** `DEEPNESS_TARGET_CLASS=2` in `.env` setzen
- **Vorteil:** Liefert Pixel-Masken — passt direkt zur Vegetations-Engine ohne Code-Änderung
- **Nachteil:** Liefert "Wald-/Vegetationsflächen", nicht einzelne Kronen. Für die
  Cluster-CO₂-Stock-Schätzung mit Connected-Components + Min/Max-Krone-Filter ausreichend;
  im Methodenteil sauber als Limitation dokumentieren.

### Option B — Tree-Tops Detection (YOLOv9)

- **Download:** <https://chmura.put.poznan.pl/s/A9zdp4mKAATEAGu>
- **Task:** Object Detection, Bounding-Boxes
- **Eingangsauflösung:** 10 cm/px (für DOP20 zu grob)
- **Tile-Größe:** 640×640
- **Status:** Code-Pfad **NICHT implementiert** — Bounding-Box-Output (YOLO-Format)
  würde NMS + BB→CD-Konvertierung statt Connected-Components benötigen.
- Bei Bedarf eigens umsetzen.

## Installiert (Stand 2026-05-25)

`tree_segmentation.onnx` (~12 MB) — **DEEPNESS LandCover.ai DeepLabV3+** ist
heruntergeladen und gemountet:

- **Architektur:** DeepLabV3+ mit tu-semnasnet_100 Backbone
- **Trainings-Datensatz:** LandCover.ai (PUTvision/LandCoverSeg)
- **Input:** 512×512 RGB, Normalisierung `pixel/255.`
- **Output-Klassen:** `0=Background`, `1=Building`, `2=Woodland`,
  `3=Water`, `4=Road`
- **Konfiguration in .env:** `DEEPNESS_TARGET_CLASS=2` (Woodland)
- **Heruntergeladen von:** <https://chmura.put.poznan.pl/s/PnAFJw27uneROkV>
- **Doku:** <https://qgis-plugin-deepness.readthedocs.io/en/latest/example/example_segmentation_landcover.html>

## Lizenz

Die DEEPNESS-Dokumentation gibt für beide Modelle "Not specified" an. **Vor der
Thesis-Abgabe direkt beim PUT Vision Lab (Posen) anfragen** und im Quellen-
Verzeichnis (`sources.md`) hinterlegen. LandCover.ai-Datensatz selbst ist
CC-BY-NC-SA 4.0 lizenziert (siehe arXiv:2005.02264) — daraus folgt eine
nicht-kommerzielle Beschränkung, die für eine Thesis aber unproblematisch ist.

## Datei-Konvention

Egal welches Modell — die Datei muss als `data/deepness/tree_segmentation.onnx`
gemountet sein. Das ist der von `settings.deepness_onnx_path` erwartete Pfad.
