export type PresetId = "archive" | "two-car" | "neutralization" | "timing" | "shared-box" | "unsupported" | "shadow";
export type Mode = "HISTORICAL_CUTOFF_REPLAY" | "DECLARED_COUNTERFACTUAL" | "HINDSIGHT_ANALYSIS" | "SHADOW_OBSERVATION" | "LIVE";
export type Operation = "forecast" | "manual" | "conditional" | "robust" | "timing";
export type Future = "GREEN" | "VSC" | "SC" | "SC_LATE";
export type Objective = "MINIMAX_REGRET" | "WORST_CASE_RELATIVE_LOSS" | "DECLARED_WEIGHTED_DELTA" | "PARETO";
export type TyreSet = "hard" | "medium" | "soft" | "used-soft";
export type State = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELED" | "UNSUPPORTED";
export type Assumptions = {
  gap_m?: number; pace_offset_s?: number; service_s?: number; extra_pit_travel_s?: number;
  vsc_at_s?: number; vsc_duration_s?: number; sc_at_s?: number; sc_duration_s?: number;
};
export type Context = {
  preset: PresetId; driver: 63 | 4; horizon: 3 | 5 | 10; mode?: Mode | null; cutoff?: string | null;
  session?: number | null; model_era?: "2023_2025" | "2026"; model_sha256?: string; assumptions?: Assumptions;
};
export type Preset = { id: PresetId; title: string; summary: string; mode: Mode; operations: Operation[] };
export type Sourced<T = unknown> = { value: T | null; provenance: string; source: string; at_utc?: string | null };
export type DriverFields = {
  identity: Sourced<{ number: number; code: string; team: string }>;
  lap: Sourced<number>;
  fitted_tyre: Sourced<{ compound_relative: string; compound_absolute: string; tyre_age_at_start: number; stint_number: number }>;
  inventory: Sourced<{ set_id: TyreSet; compound: string; initial_age_laps: number }[]>;
  classification: Sourced<number>;
  progress: Sourced<{ progress_m: number; checkpoint_index?: number }>;
  physical_predecessor: Sourced<number>;
  predecessor_gap_m: Sourced<number>;
  route: Sourced;
};
export type Model = {
  version: string; sha256: string; era: string; release: string; g3: string; calibration_cutoff_utc?: string;
};
export type Policy = { id: string; kind: string; stops: { driver: number; set_id: TyreSet; lap?: number | null; trigger?: string | null }[] };
export type Snapshot = {
  context: Context; preset: Preset; snapshot_id: string; driver_options: { id: 63 | 4; code: string; team: string }[];
  fields: {
    drivers?: DriverFields[]; model: Sourced<Model>; observed_track_state?: Sourced<string>;
    simulated_track_state?: Sourced<string>; pit_evidence?: Sourced; layout?: Sourced<{ length_m?: number }>;
    circuit?: Sourced<string>; session?: Sourced<number>; weather?: Sourced; data_freshness?: Sourced;
    historical_field?: { supported_positions: number; supported_predecessors: number; source_availability: string;
      artifact_sha256: string; drivers: { driver_number: number; classification: number | null; completed_laps: number | null;
        progress_m: number | null; route: string; quality: string; location_utc: string | null; reason: string | null }[] };
  };
  legal_policies: Policy[]; operations: Operation[]; support: string[];
  source_hashes: Record<string, string>; evaluation_marker: number[][] | null;
};
export type AnalysisRequest = {
  context: Context; snapshot_id: string; operation: Operation; policies?: string[]; tyre_sets?: TyreSet[];
  future?: Future; futures?: Future[]; objective?: Objective; weights?: number[];
  budget?: number; robust_budget?: number; max_stops?: 1 | 2;
  team_objective?: "TEAM_COMBINED" | "LEAD_DRIVER" | "TEAM_WEIGHTED";
};
export type LapMean = { lap: number; age: number; stint: number; compound: string; seconds: number; cumulative_seconds: number; support: string };
export type Forecast = { plan: string; mean: LapMean[]; model_sha256: string; feature_source_sha256: string };
export type ScoreRow = {
  candidate: string; status: string; reasons: string[]; objective_us: number | null; objective_delta_us: number | null;
  latent_racing_us: number; effective_racing_us: number; following_us: number; neutralization_us: number; restart_us: number;
  pit_route_us: number; service_wait_us: number; pit_visits: number; comparison: string;
};
export type PitVisit = { target_lap?: number; entry_us?: number; service_start_us?: number; service_end_us?: number;
  lane_exit_us?: number; merge_us?: number; rejoin_us?: number; exit_us?: number; pit_elapsed_us?: number; set_id?: string };
export type AuditRow = Record<string, string | number | boolean | null>;
export type Timeline = { pit: PitVisit[]; decisions: AuditRow[]; race_control: AuditRow[]; pit_events: AuditRow[] };
export type Score = { rows: ScoreRow[]; best: string[]; near: string[]; ranked: string[]; timelines: Record<string, Timeline> };
export type RobustRow = {
  policy: string; worst_regret_us?: number | null; worst_delta_us?: number | null;
  weighted_delta_us?: number | null; comparable: boolean; dominated_by: string[]; information_mode?: string;
};
export type TimingPolicy = {
  regret_us: number | null; delta_us: number | null; visited_pits: PitVisit[];
  first_observation_us?: number; objective_us?: number;
};
export type Timing = {
  case: Record<string, TimingPolicy>; reproduced_regret_us: number; commitments: AuditRow[]; race_control: AuditRow[];
  versions: Record<string, string>; marker_gap: AuditRow[];
};
export type Result = {
  version: "v1"; analysis_id: string; snapshot_id: string; engine_snapshot_id: string; request: AnalysisRequest;
  model: Model; evaluation_marker: number[][] | null; evidence_classification: Mode;
  scenario_family: string; executed_policies: string[]; completion: string; warnings: string[];
  recommendation: { statuses: string[]; unique_action: string | null; alternatives: string[]; near: string[] };
  source_hashes: Record<string, string>; result_sha256: string;
  payload: {
    forecasts?: Forecast[]; decision?: Score; declared?: Score; conditional?: Score;
    analysis?: { rows: RobustRow[]; regret: Record<string, Record<string, number | null>> };
    cases?: Record<string, { rows: Record<string, ScoreRow> }>;
    scenario_scripts?: Record<string, AuditRow[]>; comparator?: string;
    policies?: Record<string, { information_mode?: string; kind?: string }> | string[];
    sensitivity?: Record<string, string[]>; oracles?: Record<string, { winners: string[] }>;
    search?: Record<string, unknown>;
  } & Partial<Timing>;
};
export type Job = { id: string; state: State; cache_hit: boolean; code?: string | null; message?: string | null;
  measurements?: Record<string, number> | null; result?: Result | null };
export type Evidence = {
  model: Model & { transfer_2026: string }; physical: Record<string, string | null>; live: string;
  source_hashes: Record<string, string>; shadow_metrics: Record<string, string | number>; readiness: Record<string, unknown>;
};
export type ShadowCheckpoint = { clock: string; source_event_cutoff: string; earned_versions: number;
  seal_sha256: string; withheld: string[]; endpoints?: Record<string, Record<string, unknown>> };
export type Shadow = {
  mode: string; capture_id: string; session: number; checkpoints: ShadowCheckpoint[];
  feasibility: { minimum_global_spacing_s: number; rate_ceiling_steady_requests_per_min: number; total_required_requests_per_min: number };
  clock: { synchronization: string }; later_evaluation: Record<string, unknown>;
  baseline: { endpoints: Record<string, { max_usable_interval_s: number | null }> };
  capture: { endpoints: Record<string, { max_usable_interval_s: number | null }> };
};

const base = process.env.NEXT_PUBLIC_F1RI_API_URL ?? "http://127.0.0.1:8000/api/v1";

export function apiUrl(path: string) {
  return base + path;
}

const hash = /^[0-9a-f]{64}$/;
const states = new Set(["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELED", "UNSUPPORTED"]);

function checkEnvelope(path: string, payload: unknown) {
  if (path === "/snapshots" || path.endsWith("/snapshot")) {
    const s = payload as Partial<Snapshot> | null;
    if (!s?.snapshot_id || !hash.test(s.snapshot_id) || !s.context || !s.fields || !s.preset ||
        !Array.isArray(s.operations) || !Array.isArray(s.legal_policies) || !Array.isArray(s.support)) {
      throw new Error("The snapshot response does not match the v1 research contract.");
    }
  }
  if (path === "/analyses" || path === "/replays" || /^\/analyses\/[0-9a-f]+$/.test(path)) {
    const j = payload as Partial<Job> | null;
    if (!j?.id || !hash.test(j.id) || !j.state || !states.has(j.state) ||
        (j.state === "COMPLETED" && (j.result?.version !== "v1" || !hash.test(j.result.result_sha256) ||
          !hash.test(j.result.snapshot_id) || !j.result.payload || !j.result.recommendation))) {
      throw new Error("The analysis response does not match the v1 research contract.");
    }
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    throw new Error(problem?.error?.message ?? `Research service returned ${response.status}.`);
  }
  const payload: unknown = await response.json();
  checkEnvelope(path, payload);
  return payload as T;
}

export function post<T>(path: string, body: unknown, signal?: AbortSignal) {
  return api<T>(path, { method: "POST", body: JSON.stringify(body), signal });
}

export const active = (job: Job) => job.state === "QUEUED" || job.state === "RUNNING";
