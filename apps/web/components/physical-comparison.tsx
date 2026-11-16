"use client";

import { useEffect, useState } from "react";
import { Activity, Clock3 } from "lucide-react";
import { api, type AuditRow, type Details, type PitVisit, type Result, type Score, type Trace } from "../lib/api";
import { AuditTable, Compound, DataTable, Notice, Panel, policyName, readable, seconds } from "./ui";

export type TraceEvent = { at: number; name: string; category: "policy" | "physical" | "control"; driver?: number };
const physical = new Set(["PIT_ENTRY_COMMITTED", "SERVICE_ACTIVE", "SERVICE_COMPLETE", "REJOIN_PENDING", "REJOINED"]);
const control = new Set(["SC_COLLECTING_FIELD", "VSC_RUNNING", "GREEN_RUNNING", "SC_IN_THIS_LAP", "VSC_ENDING"]);

export function eventsFromLedger(pit: PitVisit[], decisions: AuditRow[], raceControl: AuditRow[], steps: AuditRow[]) {
  const events: TraceEvent[] = [];
  function add(at: unknown, name: string, category: TraceEvent["category"], driver?: unknown) {
    if (typeof at === "number") events.push({ at, name, category, driver: typeof driver === "number" ? driver : undefined });
  }
  pit.forEach(p => {
    add(p.entry_us, "Pit entry", "physical", p.driver);
    add(p.service_start_us, "Service begins", "physical", p.driver);
    add(p.service_end_us, "Service complete", "physical", p.driver);
    add(p.exit_us ?? p.lane_exit_us, "Pit lane exit", "physical", p.driver);
    add(p.merge_us ?? p.rejoin_us, "Track rejoin", "physical", p.driver);
  });
  for (const row of steps) {
    if (typeof row.status !== "string") continue;
    const state = row.status;
    if (state === "PIT_PLANNED" || state.includes("CANCEL") || state.includes("REVISED")) {
      add(row.at_us, readable(state), "policy", row.driver);
    }
    if (!pit.length && physical.has(state)) add(row.at_us, readable(state), "physical", row.driver);
    if (state === "APPROACHING_PIT_ENTRY" && typeof row.predicted_us === "number") {
      add(row.predicted_us, "Predicted entry deadline", "policy", row.driver);
    }
  }
  for (const row of decisions) if (typeof row.outcome === "string") {
    add(row.at_us, readable(row.outcome === "REQUESTED" ? "STOP_REQUESTED" : row.outcome), "policy", row.driver);
  }
  for (const row of raceControl) if (typeof row.after === "string" && control.has(row.after)) {
    add(row.clock_us ?? row.at_us, readable(row.after), "control");
  }
  return [...new Map(events.map(e => [`${e.at}/${e.name}/${e.driver}`, e])).values()].sort((a, b) => a.at - b.at);
}

export function EventTimeline({ lanes }: { lanes: { label: string; events: TraceEvent[] }[] }) {
  const [focus, setFocus] = useState("");
  const clocks = lanes.flatMap(lane => lane.events.map(e => e.at));
  if (!clocks.length) return <p className="muted">No pit or race-control events in these ledgers.</p>;
  const min = Math.min(...clocks), max = Math.max(...clocks);
  const visible = lanes.flatMap(lane => lane.events.map((e, i) => ({ e, label: lane.label, id: `${lane.label}:${i}` })));
  const selected = visible.find(r => r.id === focus);
  return <div className="event-timeline">
    <div className="timeline-legend"><span className="swatch policy" /> Policy action <span className="swatch physical" /> Physical execution
      <span className="swatch control" /> Race control</div>
    {lanes.map(lane => <div className="timeline-lane" key={lane.label}>
      <strong>{lane.label}</strong><div className="timeline-track">
        {lane.events.map((e, i) => <button key={i} type="button"
          className={`timeline-point ${e.category}${focus === `${lane.label}:${i}` ? " current" : ""}`}
          style={{ left: `${3 + 94 * (e.at - min) / Math.max(max - min, 1)}%` }}
          aria-label={`${lane.label} · ${e.name} · ${seconds(e.at, 6)} seconds`}
          title={`${e.name} · ${seconds(e.at, 6)} s`} onClick={() => setFocus(`${lane.label}:${i}`)} />)}
      </div>
    </div>)}
    <div className="timeline-range"><span>{seconds(min, 3)} s</span><span>{seconds(max, 3)} s</span></div>
    <p className="timeline-label" aria-live="polite">{selected ?
      `${selected.label} · ${selected.e.name}${selected.e.driver ? ` · #${selected.e.driver}` : ""} · ${seconds(selected.e.at, 6)} s`
      : "Select an event for its exact race-clock time."}</p>
    <details><summary>Exact event ledger</summary><DataTable headings={["Policy", "Event", "Driver", "Race clock (s)"]}
      rows={visible.map(r => [r.label, r.e.name, r.e.driver ?? "—", seconds(r.e.at, 6)])} /></details>
  </div>;
}

function CheckpointPlot({ traces, reference, selections, driver }: { traces: Record<string, Trace>; reference: string; selections: string[]; driver: number }) {
  const [inspected, setInspected] = useState<number>();
  const referencePoints = new Map((traces[reference]?.checkpoints ?? []).filter(p => p.driver === driver).map(p => [p.checkpoint, p]));
  const series = selections.map(id => ({
    id, points: (traces[id]?.checkpoints ?? []).filter(p => p.driver === driver && referencePoints.has(p.checkpoint))
      .map(p => ({ checkpoint: p.checkpoint, delta: p.elapsed_us - referencePoints.get(p.checkpoint)!.elapsed_us, elapsed: p.elapsed_us })),
  }));
  const all = series.flatMap(item => item.points);
  if (!all.length) return <Notice>There are no shared recorded racing checkpoints for these traces.</Notice>;
  const checkpoints = [...new Set(all.map(p => p.checkpoint))].sort((a, b) => a - b);
  const low = Math.min(0, ...all.map(p => p.delta)), high = Math.max(0, ...all.map(p => p.delta));
  const y = (value: number) => 9 + 80 * (1 - (high === low ? .5 : (value - low) / (high - low)));
  const active = inspected !== undefined && checkpoints.includes(inspected) ? inspected : checkpoints.at(-1);
  return <div className="checkpoint-plot">
    <div className="statement"><Activity size={16} /> Difference from {policyName(reference)} at shared recorded checkpoints</div>
    <div className="checkpoint-chart" role="img" aria-label="Discrete relative elapsed times by checkpoint. A data table follows.">
      <div className="checkpoint-zero" style={{ top: `${y(0)}%` }} />
      {series.map((entry, i) => entry.points.map(point => <i key={`${entry.id}-${point.checkpoint}`}
        className={`checkpoint-point series-${i}${active === point.checkpoint ? " selected" : ""}`}
        style={{ left: `${6 + 88 * checkpoints.indexOf(point.checkpoint) / Math.max(1, checkpoints.length - 1)}%`, top: `${y(point.delta)}%` }}
        title={`${policyName(entry.id)} · checkpoint ${point.checkpoint} · ${seconds(point.delta, 6)} s`} />))}
    </div>
    <div className="timeline-range"><span>Checkpoint {checkpoints[0]}</span><span>Checkpoint {checkpoints.at(-1)}</span></div>
    <label className="field">Inspect checkpoint<select aria-label="Inspect checkpoint" value={active} onChange={e => setInspected(Number(e.target.value))}>
      {checkpoints.map(cp => <option key={cp}>{cp}</option>)}</select></label>
    <DataTable headings={["Selected policy", "Elapsed (s)", "Microsecond-matched Δ (s)"]}
      rows={series.map(s => [policyName(s.id), seconds(s.points.find(p => p.checkpoint === active)?.elapsed, 6),
        seconds(s.points.find(p => p.checkpoint === active)?.delta, 6)])} />
  </div>;
}

export function PhysicalComparison({ score, result, jobId, branch, requestedReference, chooseReference, team }:
  { score: Score; result: Result; jobId: string; branch: string; requestedReference: string;
    chooseReference: (id: string) => void; team: boolean }) {
  const [details, setDetails] = useState<Details>();
  const [error, setError] = useState("");
  const [left, setLeft] = useState("");
  const [right, setRight] = useState("");
  const [driver, setDriver] = useState(result.request.context.driver);
  const complete = score.rows.filter(row => row.status === "MARKERS_REACHED" && row.objective_us !== null);
  const reference = complete.find(row => row.candidate === requestedReference) ?? complete.find(row => row.candidate === "reference");
  const selected = [left || score.rows.find(r => r.candidate !== "reference")?.candidate || "reference", right || "reference"];
  const traces = details?.branches[branch] ?? {};
  useEffect(() => {
    const abort = new AbortController();
    api<Details>(`/analyses/${jobId}/details`, { signal: abort.signal }).then(value => {
      if (value.version === "s8b-traces-v1" && value.result_sha256 === result.result_sha256 &&
          value.analysis_id === result.analysis_id) setDetails(value);
      else setError("Trace provenance did not match the selected result.");
    }).catch(e => { if (!abort.signal.aborted) setError(e.message); });
    return () => abort.abort();
  }, [jobId, result.analysis_id, result.result_sha256]);
  const unique = [...new Set(selected)];
  const lanes = unique.flatMap(id => {
    const trace = score.timelines[id];
    return trace ? [{ label: policyName(id), events: eventsFromLedger(trace.pit, trace.decisions, trace.race_control, trace.pit_events) }] : [];
  });
  return <>
    <Panel title="Common-marker results" subtitle={`${result.request.context.horizon} laps · ${result.evaluation_marker?.map(([d, m]) => `#${d} → checkpoint ${m}`).join(" / ")}`}>
      <div className="form-row">
        <label className="field">Comparison reference<select aria-label="Comparison reference" value={reference?.candidate ?? ""} onChange={e => chooseReference(e.target.value)}>
          {!reference && <option value="">No supported reference</option>}
          {complete.map(r => <option key={r.candidate} value={r.candidate}>{policyName(r.candidate)}</option>)}</select></label>
        {team && <label className="field">Checkpoint driver<select aria-label="Checkpoint driver" value={driver} onChange={e => setDriver(Number(e.target.value) as 4 | 63)}>
          {result.evaluation_marker?.map(([d]) => <option key={d} value={d}>#{d}</option>)}</select></label>}
      </div>
      {team && <Notice>Team objective · {readable(result.request.team_objective ?? "TEAM_COMBINED")}.
        The combined objective sums elapsed time for both drivers at the team marker.</Notice>}
      <DataTable headings={["Policy", "Effective total (s)", "Δ to selected ref (s)", "Clean-air total (s)", "Following (s)", "Neutralization (s)", "Result"]}
        rows={score.rows.map(r => [
          <button className="row-button" key={r.candidate} onClick={() => setLeft(r.candidate)}>{policyName(r.candidate)}</button>,
          seconds(r.objective_us), seconds(r.objective_us == null || reference?.objective_us == null ? null : r.objective_us - reference.objective_us, 6),
          seconds(r.latent_racing_us, 6), <span key="f" className="accent">{seconds(r.following_us, 6)}</span>,
          seconds(r.neutralization_us, 6), readable(r.comparison),
        ])} />
      <p className="muted">Clean-air, following, neutralization and pit quantities come from separate engine ledgers. They represent different measurements.</p>
      {error && <Notice bad>{error}</Notice>}
      {details && reference && <CheckpointPlot key={`${branch}-${driver}-${reference.candidate}`} driver={driver}
        traces={traces} reference={reference.candidate} selections={unique.filter(id => complete.some(r => r.candidate === id))} />}
    </Panel>
    <Panel title="Two-policy drilldown" subtitle={branch === "future" ? "Declared future execution" : "Cutoff-known execution"}>
      <div className="form-row">{([["First policy", selected[0], setLeft], ["Second policy", selected[1], setRight]] as const).map(([label, id, set]) =>
        <label className="field" key={label}>{label}<select aria-label={label} value={id} onChange={e => set(e.target.value)}>
          {score.rows.map(r => <option value={r.candidate} key={r.candidate}>{policyName(r.candidate)}</option>)}</select></label>)}</div>
      <div className="detail-grid">{unique.map(id => {
        const row = score.rows.find(r => r.candidate === id);
        const trace = traces[id], timing = score.timelines[id];
        if (!row || !timing) return null;
        return <div className="trace-card" key={id}>
          <h3>{policyName(id)}</h3><code>{id}</code>
          <DataTable headings={["Evidence", "Value"]} rows={[
            ["Status", readable(row.status)], ["Model", result.model.version], ["Scenario", result.scenario_family],
            ["Initial tire", trace ? Object.entries(trace.initial_tyres).map(([d, t]) => <span className="inline-tire" key={d}>
              #{d} <Compound name={t?.compound} /> · {t?.age_laps ?? "—"} laps</span>) : "Unavailable"],
            ["Planned stops", trace?.policy.stops?.map(s => `#${s.driver} · ${s.set_id} · ${s.lap ? `lap ${s.lap}` : "triggered"}`).join(" / ") || "—"],
            ["Executed stops", String(row.pit_visits)], ["Effective common-marker time (s)", seconds(row.objective_us, 6)],
            ["Following (s)", seconds(row.following_us, 6)], ["Race-control limit (s)", seconds(row.neutralization_us, 6)],
          ]} />
          {team && <DataTable headings={["Driver", "Time (s)", "Δ vs stay out (s)"]}
            rows={Object.entries(row.driver_elapsed_us).map(([d, t]) => [`#${d}`, seconds(t, 6), seconds(row.driver_delta_us[d], 6)])} />}
          <DataTable headings={["Driver", "Stop lap", "Tire set", "Mounted age", "Lane motion (s)", "Service (s)", "Shared-box wait (s)"]}
            rows={timing.pit.map(p => [p.driver ?? "—", p.target_lap ?? "—", p.set_id ?? "—",
              trace?.mounted_tyres[String(p.driver)]?.[p.set_id ?? ""]?.initial_age_laps ?? "—", seconds(p.lane_motion_us, 6),
              seconds(p.service_us, 6), seconds(p.wait_us, 6)])} />
        </div>;
      })}</div>
    </Panel>
    <Panel title="Physical pit timeline" subtitle="Exact race-clock events · discrete observations from the execution ledger">
      <EventTimeline lanes={lanes} />
      {team && <>
        <div className="statement"><Clock3 size={16} /> Team service queue · actual order by service start</div>
        <DataTable headings={["Policy", "Service order", "Driver", "Box", "Arrival (s)", "Service start (s)", "Waiting (s)"]}
          rows={unique.flatMap(id => [...(score.timelines[id]?.pit ?? [])].filter(p => p.service_start_us != null)
            .sort((a, b) => a.service_start_us! - b.service_start_us!).map((p, index) =>
              [policyName(id), index + 1, p.driver ?? "—", p.box ?? "—", seconds(p.arrival_us, 6),
                seconds(p.service_start_us, 6), seconds(p.wait_us, 6)]))} />
      </>}
      <details><summary>Raw policy decision events</summary>{unique.map(id => <AuditTable key={id} rows={score.timelines[id]?.decisions ?? []} />)}</details>
    </Panel>
  </>;
}
