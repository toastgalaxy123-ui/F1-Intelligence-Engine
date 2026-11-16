"use client";

import { ArrowRight, Binary, CalendarClock, CircleHelp, Flag, Gauge, Layers3, MapPin, Radio, ShieldCheck } from "lucide-react";
import type { Job, Snapshot } from "../lib/api";
import { Compound, DataTable, Empty, LinkButton, Notice, PaceChart, Panel, Status, policyName, readable, valueOf } from "./ui";

export type View = "Overview" | "Race State" | "Pace Forecast" | "Strategy Lab" | "Robust Strategy" | "Timing Risk" | "Research Evidence";
const utcTime = (stamp?: string | null) => stamp ? new Date(stamp).toLocaleTimeString("en-GB", {
  timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit",
}) + " UTC" : "Unavailable";

export function Overview({ snapshot, forecast, comparison, navigate, chooseDeclared }:
  { snapshot: Snapshot; forecast?: Job; comparison?: Job; navigate: (page: View) => void; chooseDeclared: () => void }) {
  const context = snapshot.context;
  const driver = snapshot.fields.drivers?.find(d => valueOf(d.identity)?.number === context.driver);
  const driverLabel = snapshot.driver_options.find(d => d.id === context.driver);
  const model = valueOf(snapshot.fields.model);
  const lap = valueOf(driver?.lap);
  const tyre = valueOf(driver?.fitted_tyre);
  const archived = context.mode === "HISTORICAL_CUTOFF_REPLAY";
  const recommendation = comparison?.result?.recommendation;
  const canCompare = snapshot.operations.includes("manual");
  const firstSeries = forecast?.result?.payload.forecasts?.slice(0, 1);
  const cards = [
    { label: "Decision cutoff", icon: CalendarClock, value: lap != null ? `Lap ${lap} complete` : "Unavailable",
      detail: utcTime(context.cutoff) },
    { label: "Forecast horizon", icon: Gauge, value: `${context.horizon} laps`,
      detail: lap != null ? `Laps ${lap + 1}–${lap + context.horizon}` : "No fitted lap anchor" },
    { label: "Fitted compound", icon: CircleHelp, value: <Compound name={tyre?.compound_relative} />,
      detail: tyre ? `${tyre.compound_absolute} · age ${tyre.tyre_age_at_start} laps` : "Tire state unavailable" },
    { label: "Physical field", icon: Layers3, value: canCompare ? "Declared field" : "Unavailable",
      detail: canCompare ? "Deterministic no-pass baseline" : archived ? "Historical strategy gated" : "Read-only evidence context" },
  ];
  return <>
    <section className="hero">
      <div className="hero-content">
        <div className="eyebrow"><Flag size={15} /> {readable(context.mode ?? "")}</div>
        <h1>{snapshot.preset.title}</h1>
        <div className="hero-meta"><span><MapPin size={15} /> {valueOf(snapshot.fields.circuit) ?? "Research evidence"}</span>
          <span>Session {context.session ?? "—"}</span><span>{driverLabel?.code} #{context.driver}</span></div>
        <p>{snapshot.preset.summary}</p>
        <div className="hero-actions">
          {snapshot.operations.includes("forecast") && <button className="primary" onClick={() => navigate("Pace Forecast")}>View pace forecast <ArrowRight size={17} /></button>}
          {canCompare && <button className="secondary" onClick={() => navigate("Strategy Lab")}>Open Strategy Lab <ArrowRight size={16} /></button>}
          {context.preset === "timing" && <button className="primary" onClick={() => navigate("Timing Risk")}>Inspect timing case <ArrowRight size={17} /></button>}
          {(context.preset === "shadow" || context.preset === "unsupported") &&
            <button className="primary" onClick={() => navigate(context.preset === "shadow" ? "Research Evidence" : "Race State")}>Inspect evidence <ArrowRight size={17} /></button>}
          {context.preset === "archive" && <button className="secondary" onClick={chooseDeclared}>Try a declared scenario</button>}
        </div>
      </div>
    </section>
    <div className="stats">
      {cards.map(({ label, icon: Icon, value, detail }) => <section className="stat" key={label}>
        <div><span>{label}</span><Icon size={15} /></div><strong>{value}</strong><small>{detail}</small>
      </section>)}
    </div>
    <div className="overview-grid">
      <Panel title="Pace outlook" subtitle={model ? `${model.version.replace("PaceV2_", "Pace V2 · ")} · clean-air continuation` : "Model-backed pace"}
        action={<Status tone="good">Mean only</Status>}>
        {firstSeries ? <PaceChart series={firstSeries} compact /> :
          <Empty title={forecast?.message ?? (snapshot.operations.includes("forecast") ? "Loading the mean forecast" : "Forecast unavailable")}
            loading={snapshot.operations.includes("forecast") && (!forecast || forecast.state === "RUNNING" || forecast.state === "QUEUED")} />}
        {firstSeries && <div className="panel-footer"><span>Last observed weather held constant</span>
          <LinkButton onClick={() => navigate("Pace Forecast")}>Pace forecast</LinkButton></div>}
      </Panel>
      <Panel title="Decision support" subtitle="Research strategy assessment" action={<ShieldCheck size={19} className="muted" />}>
        <div className="decision">
          <span className="decision-icon"><Radio size={19} /></span>
          <div><h3>{recommendation?.unique_action ? policyName(recommendation.unique_action) : "No unique supported recommendation"}</h3>
            <p>{archived ? "The archived cutoff supports a mean pace forecast. The physical predecessor, pit merge and tire-set inventory are unresolved."
              : comparison?.result ? "Inspect the evaluated alternatives and the conditions attached to the result."
              : "Select legal alternatives and run a declared comparison to evaluate decision support."}</p></div>
        </div>
        {recommendation?.alternatives.length ? <div className="chips">{recommendation.alternatives.slice(0, 6).map(p => <span key={p}>{policyName(p)}</span>)}</div> : null}
        <details><summary>Support codes</summary><div className="code-list">
          {(recommendation?.statuses ?? snapshot.support).map(code => <code key={code}>{code}</code>)}
        </div></details>
        <div className="panel-footer"><span><Binary size={14} /> {snapshot.snapshot_id.slice(0, 12)}</span>
          <LinkButton onClick={() => navigate(canCompare ? "Strategy Lab" : "Race State")}>{canCompare ? "Compare strategies" : "Race state"}</LinkButton></div>
      </Panel>
    </div>
  </>;
}

export function RaceState({ snapshot, chooseDeclared }: { snapshot: Snapshot; chooseDeclared: () => void }) {
  const drivers = snapshot.fields.drivers ?? [];
  const historical = snapshot.fields.historical_field;
  const declared = snapshot.context.mode === "DECLARED_COUNTERFACTUAL" || snapshot.context.mode === "HINDSIGHT_ANALYSIS";
  return <>
    <Panel title="Field at the cutoff" subtitle={declared ? "Declared physical state · S6-C deterministic fixture" : "Archived timing · cutoff-valid observed fields"}>
      {!declared && <Notice>Historical track-local positions, physical predecessors and exact pit merges remain uncertified. The table preserves missing observations.</Notice>}
      <DataTable headings={["Driver", "Lap completed", "Compound", "Tire age", "Progress (m)", "Physical predecessor", "Gap (m)"]}
        rows={drivers.map(d => [
          `#${valueOf(d.identity)?.number} ${valueOf(d.identity)?.code}`, valueOf(d.lap) ?? "—",
          <Compound key="tyre" name={valueOf(d.fitted_tyre)?.compound_relative} />, valueOf(d.fitted_tyre)?.tyre_age_at_start ?? "—",
          valueOf(d.progress)?.progress_m?.toFixed(2) ?? "Unavailable", valueOf(d.physical_predecessor) ?? "Unavailable",
          valueOf(d.predecessor_gap_m)?.toFixed(3) ?? "—",
        ])} />
      <div className="statement"><Flag size={16} /><span>Race control · {valueOf(snapshot.fields.simulated_track_state) ?? valueOf(snapshot.fields.observed_track_state) ?? "Unavailable"}</span></div>
      {!declared && <button className="secondary" onClick={chooseDeclared}>Open a declared counterfactual <ArrowRight size={15} /></button>}
    </Panel>
    {historical && <Panel title="S7-A historical field" subtitle={`${historical.supported_positions} supported positions · ${historical.supported_predecessors} certified predecessors · Event-time evidence`}>
      <DataTable headings={["Driver", "Classification", "Lap complete", "Progress (m)", "Route", "Source fix (UTC)", "Support"]}
        rows={historical.drivers.map(row => [row.driver_number, row.classification ?? "—", row.completed_laps ?? "—",
          row.progress_m?.toFixed(2) ?? "Unavailable", readable(row.route), row.location_utc ?? "Unavailable",
          readable(row.reason ?? row.quality)])} />
    </Panel>}
    <Panel title="Field provenance" subtitle="Observation, declared assumptions and missing fields">
      <DataTable headings={["Driver", "Field", "Classification", "Source or limitation"]}
        rows={drivers.flatMap(d => (["identity", "fitted_tyre", "progress", "physical_predecessor", "route", "inventory"] as const)
          .map(key => [`#${valueOf(d.identity)?.number}`, readable(key), readable(d[key].provenance), d[key].source]))} />
    </Panel>
  </>;
}
