# Luftbild-Raster (DOP) für die DEEPNESS-Inferenz

Lege hier ein GeoTIFF pro AOI ab. Konvention: Dateiname = AOI-Name + `.tif`,
ggf. mit slugified Sonderzeichen. Der Vegetations-Agent sucht den Pfad
über `settings.imagery_dir` (Default: `/app/data/imagery/`).

**Anforderungen:**
- GeoTIFF mit eingebetteter Georeferenz (Projection + Transform)
- Bevorzugt **EPSG:25832** (ETRS89 / UTM 32N) für Norddeutschland — sonst
  wird intern reprojiziert (langsamer)
- Mindestens RGB-Kanäle; 4-Kanal-RGBN wird automatisch auf die ersten
  drei reduziert wenn das ONNX-Modell 3 Kanäle erwartet
- Bodenauflösung passend zum Modell (DEEPNESS-Standardmodelle für
  Stadtbaum-Segmentation laufen typisch auf 5-20 cm/px)

**Bezugsquellen für die Jade-HS-AOI:**
- LGLN DOP20 (Niedersachsen, 20 cm/px) — kostenfrei nutzbar
- Bing/Esri-Aerial via QGIS-Export (für Tests)

Der Vegetations-Agent skippt mit klarer Fehlermeldung, wenn hier nichts
liegt — und der Orchestrator behandelt das Skippen als non-fatal
(B6-Pipeline läuft trotzdem durch, Spec §8).
