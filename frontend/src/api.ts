export interface SourceRef {
  label: string;
  provenance: "provided" | "benchmark" | "assumption";
  uuid?: string | null;
  citation?: string | null;
  retrieved_at?: string | null;
  version?: string | null;
}

export interface Value {
  value: number;
  unit: string;
  uncertainty?: number | null;
  source: SourceRef;
}

export interface AOI {
  name: string;
  polygon: { type: "Polygon"; coordinates: number[][][] };
  crs?: string;
}

export interface StepStatus {
  name: string;
  status: "pending" | "running" | "done" | "failed";
  started_at?: string | null;
  finished_at?: string | null;
  message?: string | null;
}

export interface EnergyResult {
  end_energy_heat_kwh: Value;
  end_energy_electricity_kwh: Value;
  penrt_kwh: Value;
  gwp_kg_co2eq: Value;
  gwp_per_ngf_kg_co2eq_per_m2a: Value;
}

export interface Building {
  osm_id: string;
  name: string | null;
  footprint_area_m2: Value;
  storeys?: Value | null;
  height_m?: Value | null;
  ngf_m2?: Value | null;
  use_mix?: Record<string, number>;
  build_year?: number | null;
  excluded: boolean;
  exclusion_reason?: string | null;
}

export interface BuildingResult {
  building: Building;
  energy: EnergyResult;
}

export interface VegetationResult {
  canopy_area_m2: Value;
  estimated_trees: number;
  co2_stock_kg: Value;
  osm_only_trees?: number;
  osm_green_area_m2?: number;
  imagery_source?: string;
}

export interface SanierungsMassnahme {
  massnahme: string;
  erwartete_einsparung: string | null;
  investitions_kategorie: string | null;
}

export interface Hotspot {
  osm_id: string;
  name: string | null;
  prioritaet: "hoch" | "mittel" | "niedrig" | string;
  begruendung: string;
  massnahmen: SanierungsMassnahme[];
}

export interface AuffaelligerBefund {
  kennzahl: string;
  befund: string;
  vermutete_ursache: string | null;
  empfohlene_pruefung: string | null;
}

export interface ReportingAdvice {
  executive_summary: string;
  cluster_assessment: string;
  hotspots: Hotspot[];
  auffaellige_befunde: AuffaelligerBefund[];
}

export interface ClusterResult {
  aoi: AOI;
  buildings: BuildingResult[];
  cluster_energy: EnergyResult;
  vegetation: VegetationResult | null;
}

export interface RunState {
  run_id: string;
  started_at: string;
  status: "pending" | "running" | "done" | "failed";
  steps: StepStatus[];
  aoi?: AOI | null;
  result?: ClusterResult | null;
  error?: string | null;
}

const BASE = "/api";

export async function health(): Promise<unknown> {
  const r = await fetch(`${BASE}/health`);
  return r.json();
}

export async function startRun(aoi: AOI): Promise<RunState> {
  const r = await fetch(`${BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(aoi),
  });
  if (!r.ok) throw new Error(`startRun: ${r.status} ${await r.text()}`);
  return r.json();
}

export async function getRun(id: string): Promise<RunState> {
  const r = await fetch(`${BASE}/runs/${id}`);
  if (!r.ok) throw new Error(`getRun: ${r.status}`);
  return r.json();
}

export async function getReport(id: string): Promise<string> {
  const r = await fetch(`${BASE}/runs/${id}/report`);
  if (!r.ok) throw new Error(`getReport: ${r.status}`);
  return r.text();
}

export function aoiRasterPreviewUrl(id: string): string {
  return `${BASE}/runs/${id}/aoi_raster_preview`;
}

export async function hasAoiRasterPreview(id: string): Promise<boolean> {
  const r = await fetch(aoiRasterPreviewUrl(id), { method: "HEAD" });
  return r.ok;
}

export function pdfDownloadUrl(id: string): string {
  return `${BASE}/runs/${id}/pdf`;
}

export async function getAdvice(id: string): Promise<ReportingAdvice | null> {
  const r = await fetch(`${BASE}/runs/${id}/advice`);
  if (!r.ok) return null;
  return r.json();
}
