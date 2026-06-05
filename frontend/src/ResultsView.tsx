import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ErrorBar,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  BuildingResult,
  ClusterResult,
  Hotspot,
  ReportingAdvice,
} from "./api";
import { getAdvice, pdfDownloadUrl } from "./api";

interface Props {
  result: ClusterResult;
  runId: string;
}

const COLORS = {
  heat: "#dc2626",        // rot — Wärme
  electricity: "#2563eb", // blau — Strom
  gwp: "#1f2937",         // dunkel — GWP
};

function buildingLabel(b: BuildingResult): string {
  if (b.building.name) return b.building.name;
  const parts = b.building.osm_id.split("/");
  return parts[parts.length - 1] ?? b.building.osm_id;
}

function fmtNumber(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return "–";
  return v.toLocaleString("de-DE", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/* ===================================================================== */
/*  B6 result view                                                        */
/* ===================================================================== */

function ClusterTable({ result }: { result: ClusterResult }) {
  const e = result.cluster_energy;
  const rows = [
    { label: "Endenergie Wärme", v: e.end_energy_heat_kwh, unit: "kWh/a", digits: 0 },
    { label: "Endenergie Strom", v: e.end_energy_electricity_kwh, unit: "kWh/a", digits: 0 },
    { label: "PENRT", v: e.penrt_kwh, unit: "kWh/a", digits: 0 },
    { label: "GWP", v: e.gwp_kg_co2eq, unit: "kg CO₂-äq./a", digits: 0 },
    { label: "GWP / NGF", v: e.gwp_per_ngf_kg_co2eq_per_m2a, unit: "kg/m²a", digits: 2 },
  ];
  return (
    <table className="result-table">
      <thead>
        <tr>
          <th>Kennzahl</th>
          <th style={{ textAlign: "right" }}>Wert ± σ</th>
          <th>Einheit</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label}>
            <td>{r.label}</td>
            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              <strong>{fmtNumber(r.v.value, r.digits)}</strong>
              {r.v.uncertainty != null && (
                <span style={{ color: "#6b7280" }}>
                  {" "}± {fmtNumber(r.v.uncertainty, r.digits)}
                </span>
              )}
            </td>
            <td style={{ color: "#6b7280" }}>{r.unit}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EnergyPerBuildingChart({ result }: { result: ClusterResult }) {
  const data = result.buildings
    .filter((b) => !b.building.excluded)
    .map((b) => ({
      name: buildingLabel(b),
      heat: Math.round(b.energy.end_energy_heat_kwh.value),
      electricity: Math.round(b.energy.end_energy_electricity_kwh.value),
    }));
  if (data.length === 0) return null;

  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
        <XAxis dataKey="name" fontSize={11} />
        <YAxis
          fontSize={11}
          tickFormatter={(v) => `${(v / 1000).toFixed(0)} k`}
          label={{ value: "kWh/a", angle: -90, position: "insideLeft", fontSize: 11 }}
        />
        <Tooltip
          formatter={(v: number) => `${fmtNumber(v)} kWh/a`}
          contentStyle={{ fontSize: 12 }}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Bar dataKey="heat" stackId="e" fill={COLORS.heat} name="Wärme" />
        <Bar dataKey="electricity" stackId="e" fill={COLORS.electricity} name="Strom" />
      </BarChart>
    </ResponsiveContainer>
  );
}

function GwpPerBuildingChart({ result }: { result: ClusterResult }) {
  const data = result.buildings
    .filter((b) => !b.building.excluded)
    .map((b) => ({
      name: buildingLabel(b),
      gwp: Math.round(b.energy.gwp_kg_co2eq.value),
      sigma: b.energy.gwp_kg_co2eq.uncertainty
        ? Math.round(b.energy.gwp_kg_co2eq.uncertainty)
        : 0,
    }));
  if (data.length === 0) return null;
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
        <XAxis dataKey="name" fontSize={11} />
        <YAxis
          fontSize={11}
          tickFormatter={(v) => `${(v / 1000).toFixed(0)} k`}
          label={{ value: "kg CO₂-äq./a", angle: -90, position: "insideLeft", fontSize: 11 }}
        />
        <Tooltip
          formatter={(v: number) => `${fmtNumber(v)} kg CO₂-äq./a`}
          contentStyle={{ fontSize: 12 }}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Bar dataKey="gwp" fill={COLORS.gwp} name="GWP">
          <ErrorBar dataKey="sigma" width={4} strokeWidth={1.5} stroke="#6b7280" />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function PerBuildingTable({ result }: { result: ClusterResult }) {
  const rows = result.buildings.filter((b) => !b.building.excluded);
  if (rows.length === 0) return null;
  return (
    <table className="result-table">
      <thead>
        <tr>
          <th>Gebäude</th>
          <th style={{ textAlign: "right" }}>Wärme [kWh/a]</th>
          <th style={{ textAlign: "right" }}>Strom [kWh/a]</th>
          <th style={{ textAlign: "right" }}>GWP [kg]</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((b) => (
          <tr key={b.building.osm_id}>
            <td>{buildingLabel(b)}</td>
            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              {fmtNumber(b.energy.end_energy_heat_kwh.value)}
            </td>
            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              {fmtNumber(b.energy.end_energy_electricity_kwh.value)}
            </td>
            <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              {fmtNumber(b.energy.gwp_kg_co2eq.value)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function VegetationBlock({ result }: { result: ClusterResult }) {
  const v = result.vegetation;
  if (!v) return null;
  return (
    <div
      style={{
        marginTop: 12,
        padding: 10,
        background: "#f0fdf4",
        border: "1px solid #bbf7d0",
        borderRadius: 4,
      }}
    >
      <strong style={{ fontSize: 13 }}>Grünraum-CO₂-Speicherung</strong>
      <ul style={{ margin: "4px 0 0 16px", padding: 0, fontSize: 12 }}>
        <li>Kronenfläche: {fmtNumber(v.canopy_area_m2.value)} m²</li>
        <li>Geschätzte Bäume: {v.estimated_trees}</li>
        <li>
          CO₂-Speicher (Stock): {fmtNumber(v.co2_stock_kg.value)} kg
          {v.co2_stock_kg.uncertainty != null && (
            <span style={{ color: "#6b7280" }}>
              {" "}± {fmtNumber(v.co2_stock_kg.uncertainty)} kg
            </span>
          )}
        </li>
      </ul>
    </div>
  );
}

/* ===================================================================== */

function priorityColor(p: string): { bg: string; border: string; text: string } {
  switch (p) {
    case "hoch": return { bg: "#fef2f2", border: "#dc2626", text: "#991b1b" };
    case "mittel": return { bg: "#fffbeb", border: "#f59e0b", text: "#92400e" };
    case "niedrig": return { bg: "#f0fdf4", border: "#16a34a", text: "#166534" };
    default: return { bg: "#f9fafb", border: "#9ca3af", text: "#374151" };
  }
}

function HotspotCard({ h, index }: { h: Hotspot; index: number }) {
  const c = priorityColor(h.prioritaet);
  const label = h.name || h.osm_id;
  return (
    <div
      style={{
        background: c.bg,
        border: `1px solid ${c.border}`,
        borderRadius: 6,
        padding: 10,
        marginBottom: 8,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <strong style={{ fontSize: 13, color: c.text }}>
          Priorität {index + 1} — {label}
        </strong>
        <span style={{ fontSize: 10, color: c.text, textTransform: "uppercase", fontWeight: 600 }}>
          {h.prioritaet}
        </span>
      </div>
      <p style={{ fontSize: 12, margin: "6px 0", color: "#374151" }}>{h.begruendung}</p>
      {h.massnahmen.length > 0 && (
        <>
          <div style={{ fontSize: 11, fontWeight: 600, color: "#374151", marginTop: 6 }}>
            Empfohlene Maßnahmen:
          </div>
          <ul style={{ margin: "4px 0 0 18px", padding: 0, fontSize: 12 }}>
            {h.massnahmen.map((m, i) => (
              <li key={i}>
                {m.massnahme}
                {m.erwartete_einsparung && (
                  <span style={{ color: "#6b7280" }}> ({m.erwartete_einsparung})</span>
                )}
                {m.investitions_kategorie && (
                  <span style={{ color: "#6b7280" }}> · Invest: {m.investitions_kategorie}</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function AdviceBlock({ advice }: { advice: ReportingAdvice }) {
  return (
    <div
      style={{
        marginBottom: 16,
        padding: 12,
        background: "#eff6ff",
        border: "1px solid #93c5fd",
        borderRadius: 6,
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: "#1e3a8a", marginBottom: 6 }}>
        KI-Empfehlungen
      </div>
      <p style={{ fontSize: 12, margin: "0 0 8px", color: "#1f2937" }}>
        {advice.executive_summary}
      </p>
      <p style={{ fontSize: 12, margin: "0 0 12px", color: "#374151", fontStyle: "italic" }}>
        {advice.cluster_assessment}
      </p>
      {advice.hotspots.length > 0 && (
        <>
          <div style={{ fontSize: 12, fontWeight: 600, color: "#1e3a8a", margin: "8px 0 4px" }}>
            Sanierungs-Priorisierung
          </div>
          {advice.hotspots.map((h, i) => (
            <HotspotCard key={h.osm_id + i} h={h} index={i} />
          ))}
        </>
      )}
      {advice.auffaellige_befunde.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ fontSize: 12, color: "#1e3a8a", cursor: "pointer", fontWeight: 600 }}>
            Auffällige Befunde anzeigen
          </summary>
          <ul style={{ margin: "6px 0 0 18px", padding: 0, fontSize: 12 }}>
            {advice.auffaellige_befunde.map((b, i) => (
              <li key={i} style={{ marginBottom: 6 }}>
                <strong>{b.kennzahl}:</strong> {b.befund}
                {b.vermutete_ursache && (
                  <div style={{ color: "#6b7280", fontSize: 11 }}>
                    Vermutete Ursache: {b.vermutete_ursache}
                  </div>
                )}
                {b.empfohlene_pruefung && (
                  <div style={{ color: "#6b7280", fontSize: 11 }}>
                    Empfohlene Prüfung: {b.empfohlene_pruefung}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
      <p style={{ fontSize: 10, color: "#6b7280", marginTop: 8, marginBottom: 0 }}>
        Hinweis: KI-gestützte Interpretation der berechneten Werte. Ersetzt keine Vor-Ort-Begehung.
      </p>
    </div>
  );
}

export default function ResultsView({ result, runId }: Props) {
  const [advice, setAdvice] = useState<ReportingAdvice | null>(null);

  useEffect(() => {
    getAdvice(runId).then(setAdvice).catch(() => setAdvice(null));
  }, [runId]);

  return (
    <div className="results-view">
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
        <a
          href={pdfDownloadUrl(runId)}
          download
          style={{
            background: "#1f2937",
            color: "white",
            padding: "6px 12px",
            borderRadius: 4,
            fontSize: 12,
            textDecoration: "none",
            fontWeight: 600,
          }}
        >
          📄 PDF herunterladen
        </a>
      </div>

      {advice && <AdviceBlock advice={advice} />}

      <h3 style={{ fontSize: 13, margin: "4px 0 8px" }}>Cluster-Bilanz</h3>
      <ClusterTable result={result} />

      <h3 style={{ fontSize: 13, margin: "16px 0 4px" }}>
        Endenergie pro Gebäude (Wärme + Strom)
      </h3>
      <EnergyPerBuildingChart result={result} />

      <h3 style={{ fontSize: 13, margin: "16px 0 4px" }}>
        Treibhausgaspotential pro Gebäude
      </h3>
      <GwpPerBuildingChart result={result} />

      <h3 style={{ fontSize: 13, margin: "16px 0 8px" }}>Pro-Gebäude-Übersicht</h3>
      <PerBuildingTable result={result} />

      <VegetationBlock result={result} />
    </div>
  );
}
