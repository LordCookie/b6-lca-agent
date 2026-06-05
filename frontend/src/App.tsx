import { useEffect, useState } from "react";
import MapSelector from "./MapSelector";
import ResultsView from "./ResultsView";
import {
  aoiRasterPreviewUrl,
  getReport,
  getRun,
  hasAoiRasterPreview,
  startRun,
  type AOI,
  type RunState,
} from "./api";

export default function App() {
  const [aoi, setAoi] = useState<AOI | null>(null);
  const [run, setRun] = useState<RunState | null>(null);
  const [report, setReport] = useState<string>("");
  const [previewReady, setPreviewReady] = useState<boolean>(false);

  useEffect(() => {
    if (!run || run.status === "done" || run.status === "failed") return;
    const t = setInterval(async () => {
      try {
        const next = await getRun(run.run_id);
        setRun(next);
        // AOI-Vorschau anzeigen, sobald der Vegetations-Schritt das PNG geschrieben hat.
        if (!previewReady) {
          const has = await hasAoiRasterPreview(next.run_id);
          if (has) setPreviewReady(true);
        }
        if (next.status === "done") {
          setReport(await getReport(next.run_id));
        }
      } catch (e) {
        console.error(e);
      }
    }, 2000);
    return () => clearInterval(t);
  }, [run?.run_id, run?.status, previewReady]);

  // Nach Lauf-Ende einmal final prüfen ob das Bild da ist — fängt den
  // Fall ab, dass der Lauf so schnell ist, dass die Polling-Schleife
  // schon beendet war bevor das PNG geschrieben wurde.
  useEffect(() => {
    if (run?.status !== "done" || previewReady) return;
    hasAoiRasterPreview(run.run_id).then((has) => {
      if (has) setPreviewReady(true);
    });
  }, [run?.status, run?.run_id, previewReady]);

  async function handleStart() {
    if (!aoi) return;
    const r = await startRun(aoi);
    setRun(r);
    setReport("");
    setPreviewReady(false);
  }

  return (
    <div className="app">
      <header>
        <h1>Cluster-Bilanzierung B6</h1>
        <span className="subtitle">
          Betriebsenergie · DIN V 18599 (quasi-stationär) · KI-gestützte Datenbeschaffung
        </span>
      </header>
      <main>
        <MapSelector onSelect={setAoi} />
        <aside className="sidebar">
          <section>
            <h2>1. Gebietsauswahl</h2>
            <p style={{ fontSize: 13, margin: "4px 0" }}>
              Liegenschaftsgrenze als Polygon zeichnen (Steuerung oben rechts auf der Karte).
            </p>
            <ol style={{ fontSize: 12, color: "#6b7280", margin: "4px 0 8px 16px", padding: 0 }}>
              <li>Auf die Karte klicken, um Eckpunkte zu setzen (max. 20)</li>
              <li>Erster Punkt erscheint <span style={{ color: "#16a34a", fontWeight: 600 }}>grün</span>; Linien zwischen den Punkten gestrichelt</li>
              <li>Bei Bedarf <em>Rückgängig</em> für letzten Punkt</li>
              <li><strong>"Polygon schließen"</strong> klicken, sobald alle Ecken stehen</li>
            </ol>
            <button disabled={!aoi || run?.status === "running"} onClick={handleStart}>
              {run?.status === "running" ? "Bilanzierung läuft…" : "Bilanzierung starten"}
            </button>
          </section>

          {run && (
            <section>
              <h2>2. Fortschritt</h2>
              <ul className="steps">
                {run.steps.map((s) => (
                  <li key={s.name} className={s.status} title={s.message || undefined}>
                    <div>
                      <span>{s.name}</span>
                      {s.message && (
                        <div style={{ fontSize: 11, color: "#6b7280", marginTop: 2, lineHeight: 1.35 }}>
                          {s.message}
                        </div>
                      )}
                    </div>
                    <span className="status">{s.status}</span>
                  </li>
                ))}
              </ul>
              {run.error && (
                <p style={{ color: "#dc2626", fontSize: 13 }}>{run.error}</p>
              )}
            </section>
          )}

          {run && previewReady && (
            <section>
              <h2>3. Luftbild der Liegenschaft</h2>
              <p style={{ fontSize: 12, color: "#6b7280", margin: "0 0 6px" }}>
                Auf die AOI zugeschnitten — Eingang der Vegetationserkennung.
              </p>
              <img
                src={aoiRasterPreviewUrl(run.run_id)}
                alt="AOI-zugeschnittener Bild-Layer"
                style={{
                  width: "100%",
                  border: "1px solid #e5e7eb",
                  borderRadius: 4,
                }}
              />
            </section>
          )}

          {run?.status === "done" && run.result && (
            <section>
              <h2>4. B6-Bilanz</h2>
              <ResultsView result={run.result} runId={run.run_id} />
            </section>
          )}

          {report && (
            <section>
              <h2>5. Vollständiger Bericht</h2>
              <pre className="report">{report}</pre>
            </section>
          )}
        </aside>
      </main>
    </div>
  );
}
