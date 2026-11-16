"use client";

import { useState } from "react";
import { ArrowRight, Download, Flag, Play, RotateCcw } from "lucide-react";
import { active, apiUrl, type AnalysisRequest, type Future, type Job, type Objective, type Operation,
  type Result, type Score, type Snapshot, type TyreSet } from "../lib/api";
import { AuditTable, Compound, DataTable, Empty, Notice, PaceChart, Panel, Status, optionLabel,
  policyName, precision, readable, seconds, valueOf } from "./ui";

export type Launch = (operation: Operation, options?: Partial<AnalysisRequest>) => void;
type Props = { snapshot: Snapshot; job?: Job; launch: Launch; cancel: () => void; invalidate: () => void };

export function JobFeedback({ job, cancel, empty = "Run an analysis to see results" }:
  { job?: Job; cancel: () => void; empty?: string }) {
  if (!job) return <Empty title={empty} detail="Results will appear here once the engine completes." />;
  if (active(job)) return <><Empty title={job.state === "QUEUED" ? "Waiting for the local worker" : "Running in the Python engine"}
    detail="Frozen sources and the current workspace inputs are attached to this analysis." loading />
    <button className="secondary" onClick={cancel}>Cancel analysis</button></>;
  if (job.state !== "COMPLETED") return <Notice bad>{job.message ?? readable(job.state)} {job.code && <code>{job.code}</code>}</Notice>;
  return null;
}

export function Reproducibility({ job, replay, replayJob, cancelReplay }:
  { job: Job; replay: (result: Result) => void; replayJob?: Job; cancelReplay: () => void }) {
  const result = job.result;
  if (!result) return null;
  return <Panel title="Run provenance" subtitle={job.cache_hit ? "Reused an identical verified result" : "Executed against the frozen research engine"}>
    <div className="provenance-ids"><span>Analysis <code>{result.analysis_id}</code></span>
      <span>Result checksum <code>{result.result_sha256}</code></span>
      <span>Snapshot <code>{result.snapshot_id}</code></span></div>
    {job.measurements && <p className="muted">{precision(job.measurements.wall_s, 2)} s wall · {precision(job.measurements.cpu_s, 2)} s CPU · {precision(job.measurements.peak_rss_mib, 0)} MiB sampled peak RSS</p>}
    <div className="actions">
      <a className="secondary" href={apiUrl(`/analyses/${job.id}/export`)}><Download size={15} /> Export result</a>
      <button className="secondary" onClick={() => replay(result)} disabled={replayJob && active(replayJob)}><RotateCcw size={15} /> Replay and verify</button>
    </div>
    {replayJob?.state === "COMPLETED" ? <div className="verified" role="status">Replay checksum verified · {replayJob.result?.result_sha256.slice(0, 16)}</div>
      : replayJob && <JobFeedback job={replayJob} cancel={cancelReplay} />}
  </Panel>;
}

export function ForecastView({ snapshot, job, launch, cancel, invalidate }: Props) {
  const [options, setOptions] = useState<TyreSet[]>(job?.result?.request.tyre_sets ?? []);
  const [visible, setVisible] = useState<string[] | null>(null);
  const driver = snapshot.fields.drivers?.find(d => valueOf(d.identity)?.number === snapshot.context.driver);
  const inventory = valueOf(driver?.inventory);
  const result = job?.result?.payload.forecasts;
  const displayed = result?.filter((row, i) => i === 0 || (visible ? visible.includes(row.plan) : i === 1 || i === 2));
  return <>
    <Panel title="Clean-air pace forecast" subtitle="D8R1 · 2023–2025 regulation era" action={<Status tone="good">Mean only</Status>}>
      <p className="muted">Last observed weather is held constant. Historical held-out errors describe model validation. Individual prediction intervals are unavailable.</p>
      {snapshot.operations.includes("forecast") ? <>
        {inventory && <div className="choice-row">
          {inventory.map(row => <label className="choice" key={row.set_id}><input type="checkbox" checked={options.includes(row.set_id)}
            onChange={e => { invalidate(); setOptions(existing => e.target.checked ? [...existing, row.set_id] : existing.filter(v => v !== row.set_id)); }} />
            <Compound compact name={row.set_id} /> {row.set_id}</label>)}
        </div>}
        <button className="primary" onClick={() => { setVisible(null); launch("forecast", { tyre_sets: options }); }}
          disabled={job && active(job)}><Play size={14} /> {inventory ? "Run selected forecast" : "Run continuation forecast"}</button>
      </> : <Notice>Forecast unavailable in the selected research context.</Notice>}
      <JobFeedback job={job} cancel={cancel} />
      {displayed && <PaceChart series={displayed} />}
      {result && result.length > 1 && <div className="choice-row">{result.slice(1).map((s, i) => <label className="choice" key={s.plan}>
        <input type="checkbox" checked={visible ? visible.includes(s.plan) : i < 2}
          onChange={e => setVisible(existing => {
            const current = existing ?? result.slice(1, 3).map(x => x.plan);
            return e.target.checked ? [...current, s.plan] : current.filter(v => v !== s.plan);
          })} />{s.plan}</label>)}</div>}
    </Panel>
    {displayed && <Panel title="Lap and stint means" subtitle="Cumulative time is within each modeled stint segment">
      <DataTable headings={["Plan", "Lap", "Compound", "Tire age", "Stint", "Mean (s)", "Cumulative (s)", "Support"]}
        rows={displayed.flatMap(s => s.mean.map(row => [s.plan, row.lap, <Compound key="c" name={row.compound} />, row.age,
          row.stint, precision(row.seconds), precision(row.cumulative_seconds), readable(row.support)]))} />
    </Panel>}
  </>;
}

function ScoreView({ score, result }: { score: Score; result: Result }) {
  const [traceId, setTraceId] = useState("");
  const id = score.timelines[traceId] ? traceId : score.rows.find(row => row.pit_visits > 0)?.candidate ?? score.rows[0]?.candidate;
  const trace = score.timelines[id];
  return <>
    <Panel title="Common-marker results" subtitle={result.evaluation_marker?.map(([driver, marker]) => `#${driver} · checkpoint ${marker}`).join("  /  ")}>
      <DataTable headings={["Policy", "Effective total (s)", "Δ vs stay out (s)", "Clean-air (s)", "Following (s)", "Neutralization (s)", "Result"]}
        rows={score.rows.map(r => [
          <button key={r.candidate} className="row-button" onClick={() => setTraceId(r.candidate)}>{policyName(r.candidate)}</button>,
          seconds(r.objective_us), seconds(r.objective_delta_us), seconds(r.latent_racing_us),
          <span key="following" className={r.following_us > 0 ? "accent" : undefined}>{seconds(r.following_us)}</span>,
          seconds(r.neutralization_us), <span key="status" title={r.reasons.join(" · ")}>{readable(r.comparison)}</span>,
        ])} />
      {!!score.best.length && <div className="statement"><Flag size={16} /> Best evaluated · {score.best.map(policyName).join(" / ")}</div>}
      {!!score.near.length && <p className="muted">Near alternatives · {score.near.map(policyName).join(" / ")}</p>}
    </Panel>
    {trace && <Panel title="Physical pit timeline" subtitle="Absolute race clock · times shown in seconds">
      <label className="field">Policy trace<select value={id} onChange={e => setTraceId(e.target.value)}>
        {score.rows.map(r => <option key={r.candidate} value={r.candidate}>{policyName(r.candidate)}</option>)}</select></label>
      {trace.pit.length ? <PitTable visits={trace.pit} /> : <p className="muted">No completed pit visit.</p>}
      <details><summary>Policy decisions and pit events</summary><AuditTable rows={trace.decisions} /><AuditTable rows={trace.pit_events} /></details>
    </Panel>}
  </>;
}

function PitTable({ visits }: { visits: NonNullable<Score["timelines"][string]["pit"]> }) {
  return <DataTable headings={["Stop lap", "Entry (s)", "Service start (s)", "Service end (s)", "Exit (s)", "Rejoin (s)", "Set"]}
    rows={visits.map(p => [p.target_lap ?? "—", seconds(p.entry_us, 6), seconds(p.service_start_us, 6),
      seconds(p.service_end_us, 6), seconds(p.lane_exit_us ?? p.exit_us, 6), seconds(p.merge_us ?? p.rejoin_us, 6),
      p.set_id ?? "—"])} />;
}

export function StrategyView({ snapshot, job, launch, cancel, invalidate, operation, chooseDeclared, selectOperation }:
  Props & { operation: "manual" | "conditional"; chooseDeclared: () => void; selectOperation: (op: "manual" | "conditional") => void }) {
  const [candidate, setCandidate] = useState("");
  const [selected, setSelected] = useState<string[]>(job?.result?.request.policies ?? []);
  const [future, setFuture] = useState<Future>(job?.result?.request.future ?? "GREEN");
  const [budget, setBudget] = useState(job?.result?.request.budget ?? 32);
  const [basis, setBasis] = useState("decision");
  const enabled = snapshot.operations.includes(operation);
  const available = snapshot.legal_policies.filter(p => p.stops.length && !selected.includes(p.id));
  const score = job?.result?.payload;
  const current = basis === "decision" ? score?.decision : score?.declared ?? score?.conditional;
  return <>
    <Panel title="Strategy comparison" subtitle="Same initial state, legal tire sets and a common evaluation marker">
      {!enabled ? <><Notice>Physical strategy execution is unavailable for this research context. Historical tires and pit geometry cannot be established.</Notice>
        <button className="secondary" onClick={chooseDeclared}>Open a declared counterfactual <ArrowRight size={14} /></button></> : <>
        <div className="segmented" aria-label="Strategy operation">{(["manual", "conditional"] as const).map(mode =>
          <button key={mode} className={operation === mode ? "selected" : ""} onClick={() => selectOperation(mode)}>
            {mode === "manual" ? "Manual comparison" : "Conditional optimization"}</button>)}</div>
        <div className="form-row">
          {operation === "manual" && <><label className="field">Legal stop<select value={candidate} onChange={e => setCandidate(e.target.value)}>
            <option value="">Choose a tire set and lap</option>
            {available.map(p => <option value={p.id} key={p.id}>{optionLabel(p)}</option>)}</select></label>
            <button className="secondary" disabled={!candidate || selected.length >= 5}
              onClick={() => { invalidate(); setSelected([...selected, candidate]); setCandidate(""); }}>Add alternative</button></>}
          <label className="field">Declared future<select value={future} onChange={e => { invalidate(); setFuture(e.target.value as Future); }}>
            <option>GREEN</option><option>VSC</option><option>SC</option><option value="SC_LATE">SC late</option>
          </select></label>
          {operation === "conditional" && <label className="field">Search budget<input type="number" min="2" max="128" value={budget} onChange={e => { invalidate(); setBudget(Number(e.target.value)); }} /></label>}
        </div>
        {operation === "manual" && <div className="chips strategy-chips"><span>Stay out · reference</span>
          {selected.map(p => <button key={p} onClick={() => { invalidate(); setSelected(selected.filter(item => item !== p)); }}
            aria-label={`Remove ${policyName(p)}`}>{policyName(p)} ×</button>)}</div>}
        <button className="primary" disabled={Boolean(job && active(job)) || (operation === "conditional" && (budget < 2 || budget > 128))}
          onClick={() => { setBasis("decision"); launch(operation, { future, ...(operation === "manual" ? { policies: selected } : { budget }) }); }}>
          <Play size={14} /> {operation === "manual" ? "Run comparison" : "Run conditional search"}</button>
      </>}
      {enabled && <JobFeedback job={job} cancel={cancel} empty={operation === "manual" ? "No strategy comparison yet" : "No conditional search yet"} />}
    </Panel>
    {job?.result && <>
      <Recommendation result={job.result} />
      <Panel title="Evaluation information">
        <div className="segmented" aria-label="Result clocks">
          <button className={basis === "decision" ? "selected" : ""} onClick={() => setBasis("decision")}>
            {snapshot.context.mode === "HINDSIGHT_ANALYSIS" ? "Retrospective" : "Known at cutoff"}</button>
          <button className={basis === "future" ? "selected" : ""} onClick={() => setBasis("future")}>Declared future</button>
        </div>
        {score?.search && <p className="muted">Evaluated {String(score.search.evaluated)} of {String(score.search.legal)} legal policies.</p>}
      </Panel>
      {current && <ScoreView score={current} result={job.result} />}
    </>}
  </>;
}

export function Recommendation({ result }: { result: Result }) {
  const rec = result.recommendation;
  return <Panel title="Recommendation support" action={<Status tone={rec.unique_action ? "good" : "warm"}>{readable(result.completion)}</Status>}>
    <h3>{rec.unique_action ? policyName(rec.unique_action) : "No unique supported recommendation"}</h3>
    {rec.statuses.some(x => x.includes("INCOMPLETE")) && <Notice>The search covered a bounded subset. Rankings apply only to evaluated policies.</Notice>}
    {rec.alternatives.length > 0 && <div className="chips">{rec.alternatives.map(p => <span key={p}>{policyName(p)}</span>)}</div>}
    <ul className="status-list">{rec.statuses.filter(s => s !== "NO_UNIQUE_RECOMMENDATION").map(s => <li key={s}>{readable(s)}</li>)}</ul>
    <details><summary>Technical status codes</summary><div className="code-list">{rec.statuses.map(s => <code key={s}>{s}</code>)}</div></details>
  </Panel>;
}

const futureOptions: Future[] = ["GREEN", "VSC", "SC", "SC_LATE"];
const objectives: Objective[] = ["MINIMAX_REGRET", "WORST_CASE_RELATIVE_LOSS", "DECLARED_WEIGHTED_DELTA", "PARETO"];

export function RobustView({ snapshot, job, launch, cancel, invalidate, chooseDeclared }: Props & { chooseDeclared: () => void }) {
  const [futures, setFutures] = useState<Future[]>(job?.result?.request.futures ?? ["GREEN", "VSC", "SC"]);
  const [objective, setObjective] = useState<Objective>(job?.result?.request.objective ?? "MINIMAX_REGRET");
  const [budget, setBudget] = useState(job?.result?.request.robust_budget ?? 100);
  const [weights, setWeights] = useState<Partial<Record<Future, number>>>({});
  const result = job?.result;
  const data = result?.payload;
  const evaluated = Object.keys(data?.cases ?? {});
  const canRun = snapshot.operations.includes("robust");
  return <>
    <Panel title="Declared futures" subtitle="Select scenarios and a comparison objective">
      {canRun ? <>
        <div className="choice-row">{futureOptions.map(flag => <label className={`choice flag ${flag.toLowerCase()}`} key={flag}>
          <input type="checkbox" checked={futures.includes(flag)} onChange={e => { invalidate(); setFutures(e.target.checked ? [...futures, flag] : futures.filter(f => f !== flag)); }} />
          {flag.replace("_", " ")}</label>)}</div>
        <div className="form-row"><label className="field">Comparison objective<select value={objective} onChange={e => { invalidate(); setObjective(e.target.value as Objective); }}>
          {objectives.map(o => <option key={o} value={o}>{readable(o)}</option>)}</select></label>
          <label className="field">Robust search budget<input type="number" value={budget} min="4" max="240" onChange={e => { invalidate(); setBudget(Number(e.target.value)); }} /></label>
          {objective === "DECLARED_WEIGHTED_DELTA" && futures.map(f => <label className="field" key={f}>{f} weight
            <input type="number" min="1" max="10" value={weights[f] ?? 1} onChange={e => { invalidate(); setWeights({ ...weights, [f]: Number(e.target.value) }); }} /></label>)}
        </div>
        <button className="primary" disabled={!futures.length || budget < 4 || budget > 240 || Boolean(job && active(job))}
          onClick={() => launch("robust", { futures, objective, budget: 128, robust_budget: budget,
            weights: objective === "DECLARED_WEIGHTED_DELTA" ? futures.map(f => weights[f] ?? 1) : [] })}>
          <Play size={14} /> Run robust comparison</button>
        <JobFeedback job={job} cancel={cancel} empty="No robust comparison yet" />
      </> : <><Notice>The current context lacks the declared physical inputs needed for robust strategy comparison.</Notice>
        <button className="secondary" onClick={chooseDeclared}>Open a declared counterfactual</button></>}
    </Panel>
    {result && data && <>
      <Recommendation result={result} />
      <Panel title="Policy regret by scenario" subtitle={`Regret (s) at common markers · comparator ${data.comparator ?? "unavailable"}`}>
        <DataTable headings={["Executable policy", "Policy behavior", ...evaluated, "Worst regret (s)", "Worst relative loss (s)", "Pareto"]}
          rows={(data.analysis?.rows ?? []).map(r => [policyName(r.policy),
            data.policies && !Array.isArray(data.policies) ? readable(data.policies[r.policy]?.kind ?? "Unknown") : "—",
            ...evaluated.map(f => seconds(data.analysis?.regret[r.policy]?.[f])),
            seconds(r.worst_regret_us), seconds(r.worst_delta_us), !r.comparable ? "—" : r.dominated_by.length ? "No" : "Yes"])} />
      </Panel>
      <Panel title="Scenario-by-policy outcomes" subtitle="Effective elapsed seconds from the declared initial state">
        <DataTable headings={["Policy", ...evaluated]} rows={(data.analysis?.rows ?? []).map(r => [
          policyName(r.policy), ...evaluated.map(f => seconds(data.cases?.[f]?.rows[r.policy]?.objective_us)),
        ])} />
        <details><summary>Declared control scripts and conditional winners</summary>
          {evaluated.map(k => <div key={k}><h3>{k}</h3><p>{data.sensitivity?.[k]?.map(policyName).join(" / ")}</p>
            <AuditTable rows={data.scenario_scripts?.[k] ?? []} /></div>)}
        </details>
        <details><summary>Retrospective perfect-information oracle</summary>
          <p className="muted">Hindsight benchmark · excluded from executable recommendations.</p>
          <DataTable headings={["Future", "Oracle winners"]} rows={Object.entries(data.oracles ?? {}).map(([k, row]) => [k, row.winners?.map(policyName).join(" / ") ?? "—"])} />
        </details>
      </Panel>
    </>}
  </>;
}

export function TimingView({ snapshot, job, launch, cancel, choose }: Props & { choose: () => void }) {
  const [policy, setPolicy] = useState("sc-63-soft");
  const data = job?.result?.payload;
  const control = (data?.race_control ?? []).filter(event => event.policy === policy);
  const commitment = (data?.commitments ?? []).filter(event => event.policy === policy);
  const visit = data?.case?.[policy]?.visited_pits[0];
  const service = commitment.find(row => row.status === "SERVICE_QUEUE");
  const timeline = [
    ["SC deployed", control.find(row => row.after === "SC_COLLECTING_FIELD")?.clock_us],
    ["Stop selected · policy information clock", commitment.find(row => row.status === "REQUESTED")?.at_us],
    ["GREEN resumed", control.find(row => row.after === "GREEN_RUNNING")?.clock_us],
    ["Physical pit entry", visit?.entry_us],
    ["Service began", visit?.service_start_us],
    ["Service completed", service?.service_end_us],
    ["Pit lane exit", service?.lane_exit_us],
    ["Track rejoin", visit?.merge_us],
  ] as const;
  return <>
    <Panel title="Safety Car timing failure" subtitle="Frozen S6-B.1 · B-stress / sc-deploy-58" action={<Status tone="warm">Retrospective</Status>}>
      <p className="muted">The original reactive policy received the SC signal after its lap-31 entry opportunity. It entered on lap 32 after GREEN resumed. The scheduled conditional policy follows a separately tested commitment rule.</p>
      {snapshot.operations.includes("timing") ? <>
        <button className="primary" onClick={() => launch("timing")} disabled={job && active(job)}><Play size={14} /> Reproduce timing case</button>
        <JobFeedback job={job} cancel={cancel} empty="The frozen comparison is ready to reproduce" />
      </> : <button className="secondary" onClick={choose}>Select the timing preset <ArrowRight size={14} /></button>}
      {data?.reproduced_regret_us != null && <div className="timing-number"><span>Original reactive policy regret</span>
        <strong>{seconds(data.reproduced_regret_us, 6)} s</strong><small>Recomputed by the frozen physical simulator</small></div>}
    </Panel>
    {data?.case && <>
      <Panel title="Reactive and scheduled alternatives" subtitle={Object.values(data.versions ?? {}).join(" · ")}>
        <DataTable headings={["Policy", "Actual pit lap", "Δ vs stay out (s)", "Regret (s)"]}
          rows={Object.entries(data.case).map(([id, row]) => [
            <button key={id} className="row-button" onClick={() => setPolicy(id)}>{id}</button>, row.visited_pits[0]?.target_lap ?? "—",
            seconds(row.delta_us, 6), seconds(row.regret_us, 6),
          ])} />
      </Panel>
      <Panel title="Physical pit timeline" subtitle="Absolute race clock · deployment, observation, commitment and rejoin">
        <label className="field">Frozen policy trace<select value={policy} onChange={e => setPolicy(e.target.value)}>
          {Object.keys(data.case).map(id => <option key={id}>{id}</option>)}</select></label>
        <DataTable headings={["Event", "Race clock (s)"]} rows={timeline.map(([name, clock]) =>
          [name, seconds(typeof clock === "number" ? clock : null, 6)])} />
        {policy === "sc-63-soft" && <Notice>In this declared case, the original reactive policy observes the
          SC deployment when it requests the lap-32 stop. GREEN resumes before physical entry.</Notice>}
        <details><summary>Original commitment and race-control ledgers</summary>
          <h3>Policy commitment</h3><AuditTable rows={commitment} />
          <h3>Race-control events</h3><AuditTable rows={control} /></details>
      </Panel>
    </>}
  </>;
}
