import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import type { AOI } from "./api";

interface Props {
  onSelect: (aoi: AOI) => void;
}

// Jade HS Oldenburg roughly here — spec §8: AOI = Liegenschaftsgrenze.
const JADE_CENTER: [number, number] = [53.1435, 8.2147];

// Hard cap — UI also disables further clicks once reached.
const MAX_VERTICES = 20;

/**
 * Custom polygon-drawing tool. Every map click appends a vertex; the polygon
 * is only closed when the user explicitly clicks "Polygon schließen".
 *
 * We rolled our own here because Leaflet.Draw closes the polygon on click of
 * the first vertex, which feels fragile and tends to auto-close after 3 clicks
 * if the user accidentally lands near the first point.
 */
export default function MapSelector({ onSelect }: Props) {
  const mapRef = useRef<HTMLDivElement | null>(null);
  const mapInstance = useRef<L.Map | null>(null);
  const drawnLayer = useRef<L.FeatureGroup | null>(null);

  const [points, setPoints] = useState<L.LatLng[]>([]);
  const [closed, setClosed] = useState(false);

  // Refs mirror the latest state so the once-installed click handler can read
  // up-to-date values without re-binding on every render.
  const closedRef = useRef(closed);
  const pointsLenRef = useRef(0);
  useEffect(() => {
    closedRef.current = closed;
    pointsLenRef.current = points.length;
  }, [closed, points]);

  // --- Map init (once) -----------------------------------------------------
  useEffect(() => {
    if (mapInstance.current || !mapRef.current) return;

    const map = L.map(mapRef.current).setView(JADE_CENTER, 17);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);

    const drawn = new L.FeatureGroup();
    drawn.addTo(map);

    mapInstance.current = map;
    drawnLayer.current = drawn;

    map.on("click", (e: L.LeafletMouseEvent) => {
      if (closedRef.current) return;
      if (pointsLenRef.current >= MAX_VERTICES) return;
      setPoints((prev) => [...prev, e.latlng]);
    });
  }, []);

  // --- Redraw layer whenever points/closed change --------------------------
  useEffect(() => {
    const drawn = drawnLayer.current;
    if (!drawn) return;
    drawn.clearLayers();

    // Vertex markers — first one in green so the user sees where it began.
    points.forEach((p, i) => {
      L.circleMarker(p, {
        radius: i === 0 ? 7 : 5,
        color: i === 0 ? "#16a34a" : "#1d4ed8",
        fillColor: i === 0 ? "#16a34a" : "#3b82f6",
        fillOpacity: 1,
        weight: 2,
      })
        .bindTooltip(`Punkt ${i + 1}`, { permanent: false })
        .addTo(drawn);
    });

    // Connecting lines while drawing (open polyline) — or closed polygon.
    if (closed && points.length >= 3) {
      L.polygon(points, {
        color: "#2563eb",
        weight: 2,
        fillColor: "#3b82f6",
        fillOpacity: 0.25,
      }).addTo(drawn);
    } else if (points.length >= 2) {
      L.polyline(points, {
        color: "#2563eb",
        weight: 2,
        dashArray: "6 4",
      }).addTo(drawn);
    }
  }, [points, closed]);

  // --- Actions -------------------------------------------------------------
  function undoPoint() {
    setClosed(false);
    setPoints((prev) => prev.slice(0, -1));
  }

  function reset() {
    setClosed(false);
    setPoints([]);
  }

  function closePolygon() {
    if (points.length < 3 || closed) return;
    setClosed(true);
    const ring = [...points, points[0]].map((p) => [p.lng, p.lat]);
    onSelect({
      name: "AOI",
      polygon: { type: "Polygon", coordinates: [ring] },
      crs: "EPSG:4326",
    });
  }

  const canClose = points.length >= 3 && !closed;
  const atMax = points.length >= MAX_VERTICES;

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <div ref={mapRef} style={{ width: "100%", height: "100%" }} />

      {/* Floating control panel — top right */}
      <div
        style={{
          position: "absolute",
          top: 10,
          right: 10,
          background: "white",
          border: "1px solid #e5e7eb",
          borderRadius: 6,
          boxShadow: "0 2px 6px rgba(0,0,0,0.12)",
          padding: 10,
          display: "flex",
          flexDirection: "column",
          gap: 6,
          fontSize: 12,
          minWidth: 220,
          zIndex: 1000,
        }}
      >
        <div style={{ fontWeight: 600, color: "#1f2937" }}>
          AOI-Polygon zeichnen
        </div>
        <div style={{ color: "#6b7280", lineHeight: 1.35 }}>
          Auf die Karte klicken, um Punkte zu setzen. Punkt 1 ist <span style={{ color: "#16a34a", fontWeight: 600 }}>grün</span>.
          Wenn alle Ecken gesetzt sind: <em>Polygon schließen</em>.
        </div>
        <div style={{ color: atMax ? "#dc2626" : "#374151" }}>
          {points.length} / {MAX_VERTICES} Punkte
          {closed && (
            <span style={{ marginLeft: 6, color: "#16a34a", fontWeight: 600 }}>
              ✓ geschlossen
            </span>
          )}
        </div>
        <button
          onClick={closePolygon}
          disabled={!canClose}
          style={{
            background: canClose ? "#16a34a" : "#d1d5db",
            color: "white",
            border: "none",
            padding: "8px 12px",
            borderRadius: 4,
            cursor: canClose ? "pointer" : "not-allowed",
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          {closed ? "Polygon geschlossen" : "Polygon schließen"}
        </button>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            onClick={undoPoint}
            disabled={points.length === 0}
            style={{
              flex: 1,
              background: "white",
              color: "#1f2937",
              border: "1px solid #d1d5db",
              padding: "6px 8px",
              borderRadius: 4,
              cursor: points.length === 0 ? "not-allowed" : "pointer",
              fontSize: 12,
            }}
          >
            ↶ Rückgängig
          </button>
          <button
            onClick={reset}
            disabled={points.length === 0}
            style={{
              flex: 1,
              background: "white",
              color: "#991b1b",
              border: "1px solid #fecaca",
              padding: "6px 8px",
              borderRadius: 4,
              cursor: points.length === 0 ? "not-allowed" : "pointer",
              fontSize: 12,
            }}
          >
            🗑 Zurücksetzen
          </button>
        </div>
        {atMax && !closed && (
          <div style={{ color: "#dc2626", fontSize: 11 }}>
            Maximum erreicht — bitte schließen oder zurücksetzen.
          </div>
        )}
      </div>
    </div>
  );
}
