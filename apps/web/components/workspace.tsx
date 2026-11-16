"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Activity, ArrowUpRight, ChevronDown, ClipboardList, Flag, FlaskConical, FolderOpen, Gauge,
  LayoutGrid, Menu, Radio, Route, ShieldCheck, Timer, Workflow, X } from "lucide-react";
import { active, api, post, type AnalysisRequest, type Context, type Job, type Operation, type Preset, type PresetId,
  type Result, type Snapshot } from "../lib/api";
import { ForecastView, JobFeedback, Reproducibility, RobustView, StrategyView, TimingView } from "./analysis-views";
import { ResearchEvidence } from "./evidence";
import { Overview, RaceState, type View } from "./overview";
import { DataTable, Empty, Notice, Panel, readable, seconds } from "./ui";
import { WorkspaceShelf } from "./workspace-shelf";
import { type WorkspaceFile, workspaceVersion } from "../lib/workspaces";

const nav = [
  { name: "Overview", icon: LayoutGrid, description: "Selected scenario and decision support" },
  { name: "Race State", icon: Route, description: "Cutoff-visible timing, tires and physical evidence" },
  { name: "Pace Forecast", icon: Activity, description: "D8R1 clean-air stint trajectories" },
  { name: "Strategy Lab", icon: FlaskConical, description: "Candidate selection and deterministic simulation" },
  { name: "Robust Strategy", icon: Workflow, description: "Policy outcomes across declared race-control futures" },
  { name: "Timing Risk", icon: Timer, description: "Safety Car observation and pit-entry commitment" },
  { name: "Research Evidence", icon: ClipboardList, description: "Source, model and decision provenance" },
  { name: "Workspaces", icon: FolderOpen, description: "Save, restore and reproduce an investigation" },
] as const;
type JobKey = Operation | "replay";
type Loaded = { key: string; data?: Snapshot; error?: string };

export function Workspace() {
  const [view, setView] = useState<View>("Overview");
  const [presets, setPresets] = useState<Preset[]>([]);
  const [catalogError, setCatalogError] = useState("");
  const [generation, setGeneration] = useState(0);
  const [context, setContext] = useState<Context>({ preset: "archive", driver: 63, horizon: 5 });
  const [loaded, setLoaded] = useState<Loaded>();
  const [jobs, setJobs] = useState<Partial<Record<JobKey, Job>>>({});
  const [drafts, setDrafts] = useState<Partial<Record<Operation, Partial<AnalysisRequest>>>>({});
  const [submitted, setSubmitted] = useState<Partial<Record<Operation, AnalysisRequest>>>({});
  const [lastOperation, setLastOperation] = useState<Operation>();
  const [previous, setPrevious] = useState<Result>();
  const [reference, setReference] = useState("reference");
  const [reopened, setReopened] = useState<WorkspaceFile>();
  const [editingContext, setEditingContext] = useState(false);
  const [editorVersion, setEditorVersion] = useState(0);
  const [comparison, setComparison] = useState<"manual" | "conditional">("manual");
  const [menu, setMenu] = useState(false);
  const [browse, setBrowse] = useState(false);
  const controllers = useRef<Partial<Record<JobKey, AbortController>>>({});
  const cancelRequests = useRef(new WeakSet<AbortController>());
  const contextKey = JSON.stringify(context);
  const snapshot = loaded?.key === contextKey ? loaded.data : undefined;
  const snapshotError = loaded?.key === contextKey ? loaded.error : undefined;
  const preset = presets.find(p => p.id === context.preset);

  useEffect(() => {
    const abort = new AbortController();
    api<Preset[]>("/presets", { signal: abort.signal }).then(setPresets).catch(e => {
      if (!abort.signal.aborted) setCatalogError(e.message);
    });
    return () => abort.abort();
  }, [generation]);
  useEffect(() => {
    const abort = new AbortController();
    post<Snapshot>("/snapshots", JSON.parse(contextKey), abort.signal).then(data => {
      if (!abort.signal.aborted) setLoaded({ key: contextKey, data });
    }).catch(e => {
      if (!abort.signal.aborted) setLoaded({ key: contextKey, error: e.message });
    });
    return () => abort.abort();
  }, [contextKey, generation]);
  useEffect(() => {
    const activeControllers = controllers.current;
    return () => Object.values(activeControllers).forEach(c => c.abort());
  }, []);

  const start = useCallback(async (key: JobKey, path: string, body: unknown) => {
    controllers.current[key]?.abort();
    const abort = new AbortController();
    controllers.current[key] = abort;
    const update = (job: Job) => {
      if (!abort.signal.aborted) setJobs(existing => ({ ...existing, [key]: job }));
    };
    update({ id: "", state: "QUEUED", cache_hit: false });
    try {
      let job = await post<Job>(path, body, abort.signal);
      update(job);
      while (active(job) && !abort.signal.aborted) {
        await new Promise(resolve => setTimeout(resolve, 170));
        if (abort.signal.aborted) break;
        const cancelNow = cancelRequests.current.delete(abort);
        job = await api<Job>(`/analyses/${job.id}`, { signal: abort.signal, method: cancelNow ? "DELETE" : "GET" });
        update(job);
      }
    } catch (cause) {
      if (!abort.signal.aborted) update({ id: "", state: "FAILED", cache_hit: false,
        message: cause instanceof Error ? cause.message : "The local research request failed." });
    }
  }, []);

  const launch = useCallback((operation: Operation, options: Partial<AnalysisRequest> = {}) => {
    if (!snapshot?.operations.includes(operation)) return;
    controllers.current.replay?.abort();
    setJobs(existing => ({ ...existing, replay: undefined }));
    const request = { ...options, context: snapshot.context, snapshot_id: snapshot.snapshot_id, operation };
    setSubmitted(existing => ({ ...existing, [operation]: request }));
    if (operation !== "forecast") setLastOperation(operation);
    void start(operation, "/analyses", request);
  }, [snapshot, start]);

  useEffect(() => {
    if (!snapshot?.operations.includes("forecast") || reopened?.analysis?.operation === "forecast") return;
    const timer = setTimeout(() => launch("forecast", { tyre_sets: [] }), 0);
    return () => clearTimeout(timer);
  }, [launch, snapshot, reopened]);

  function invalidate(key: JobKey) {
    const prior = jobs[key]?.result;
    if (prior && key !== "replay") setPrevious(prior);
    controllers.current[key]?.abort();
    delete controllers.current[key];
    setJobs(old => ({ ...old, [key]: undefined, replay: undefined }));
  }

  function cancel(key: JobKey) {
    const controller = controllers.current[key];
    if (controller) cancelRequests.current.add(controller);
  }

  function change(next: Context) {
    setEditingContext(false);
    if (JSON.stringify(next) === contextKey) return;
    const prior = Object.values(jobs).filter(job => job?.result).at(-1)?.result;
    if (prior) setPrevious(prior);
    Object.values(controllers.current).forEach(c => c.abort());
    setJobs({});
    setDrafts({});
    setSubmitted({});
    setLastOperation(undefined);
    setReopened(undefined);
    setReference("reference");
    setContext(next);
  }

  function select(id: PresetId, nextView?: View) {
    change({ preset: id, driver: id === "timing" ? 63 : context.driver, horizon: id === "timing" ? 5 : context.horizon });
    setBrowse(false);
    if (nextView) navigate(nextView);
  }

  function navigate(next: View) {
    if (next === "Pace Forecast") setLastOperation("forecast");
    if (next === "Strategy Lab") setLastOperation(comparison);
    if (next === "Robust Strategy") setLastOperation("robust");
    if (next === "Timing Risk") setLastOperation("timing");
    setView(next);
    setMenu(false);
    setBrowse(false);
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function applyAssumptions(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const entered = (key: string) => Number(values.get(key));
    change({ ...context, mode: String(values.get("mode")) as Context["mode"],
      session: Number(values.get("session")) || undefined,
      model_era: String(values.get("era")) as Context["model_era"],
      cutoff: String(values.get("cutoff") ?? "").trim() || undefined,
      assumptions: { gap_m: entered("gap"), service_s: entered("service"), pace_offset_s: entered("pace"),
        extra_pit_travel_s: entered("pit"), vsc_at_s: entered("vscAt"), vsc_duration_s: entered("vscWindow"),
        sc_at_s: entered("scAt"), sc_duration_s: entered("scWindow") },
    });
  }

  function verify(result: Result) {
    void start("replay", "/replays", { request: result.request, expected_sha256: result.result_sha256 });
  }
  const op: Operation | undefined = view === "Pace Forecast" ? "forecast" : view === "Strategy Lab" ? comparison :
    view === "Robust Strategy" ? "robust" : view === "Timing Risk" ? "timing" : undefined;
  const descriptor = nav.find(item => item.name === view);
  const declared = () => select("two-car", view === "Race State" ? "Race State" : "Strategy Lab");
  const chosen = op && !editingContext ? jobs[op] : undefined;
  const jobProps = op ? { job: chosen, launch, cancel: () => cancel(op), invalidate: () => invalidate(op),
    draft: drafts[op], edit: (fields: Partial<AnalysisRequest>) => {
      invalidate(op);
      setReopened(undefined);
      setDrafts(existing => ({ ...existing, [op]: { ...existing[op], ...fields } }));
      if (fields.policies && !fields.policies.includes(reference)) setReference("reference");
    } } :
    { job: undefined, launch, cancel: () => undefined, invalidate: () => undefined };
  const timing = context.preset === "timing";
  const contextOptions = context.assumptions ?? {};

  function buildWorkspace(title: string): WorkspaceFile {
    if (!snapshot || editingContext) throw new Error("Apply the selected context before saving.");
    const operation = op ?? lastOperation;
    const result = operation ? jobs[operation]?.result : undefined;
    const remembered = operation && snapshot.operations.includes(operation) ? {
      ...submitted[operation], ...drafts[operation], context: snapshot.context, snapshot_id: snapshot.snapshot_id, operation,
    } : null;
    const sha = snapshot.fields.model.value?.sha256;
    if (!sha) throw new Error("The current model provenance is unavailable.");
    const now = new Date().toISOString();
    const analysis = result?.request ?? remembered;
    return { version: workspaceVersion, id: crypto.randomUUID(), title: title || snapshot.preset.title,
      created_at: now, updated_at: now, context: snapshot.context, snapshot_id: snapshot.snapshot_id,
      model_sha256: sha, source_hashes: snapshot.source_hashes, support: snapshot.support,
      analysis, receipt: result ? { analysis_id: result.analysis_id, result_sha256: result.result_sha256 } : null,
      reference: analysis?.policies?.includes(reference) ? reference : "reference" };
  }

  function reopen(doc: WorkspaceFile) {
    change(doc.context);
    Object.values(controllers.current).forEach(c => c.abort());
    setJobs({});
    setSubmitted({});
    setReference(doc.reference);
    setReopened(doc);
    const operation = doc.analysis?.operation;
    setDrafts(doc.analysis && operation ? { [operation]: doc.analysis } : {});
    if (operation === "manual" || operation === "conditional") setComparison(operation);
    navigate(operation === "manual" || operation === "conditional" ? "Strategy Lab" : operation === "robust" ?
      "Robust Strategy" : operation === "timing" ? "Timing Risk" : operation === "forecast" ? "Pace Forecast" : "Overview");
    setLastOperation(operation);
  }

  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to workspace</a>
    <aside className={`sidebar${menu ? " mobile-open" : ""}`}>
      <button className="brand" onClick={() => navigate("Overview")} aria-label="F1 Race Intelligence home">
        <span className="brand-mark"><Flag size={24} strokeWidth={2.5} /></span>
        <span><strong>F1 RACE INTELLIGENCE</strong><small>Research workspace</small></span>
      </button>
      <nav aria-label="Research workspace">{nav.map(({ name, icon: Icon }) => <button className={view === name ? "nav-active" : ""}
        aria-current={view === name ? "page" : undefined} key={name} onClick={() => navigate(name)}>
        <Icon size={17} strokeWidth={1.75} /><span>{name}</span>{view === name && <span className="nav-dot" />}</button>)}</nav>
      <div className="side-bottom"><div className="side-status"><span className="indicator" /><div><strong>Private research</strong><small>Local workspace · API v1</small></div></div>
        <button className="side-evidence" onClick={() => navigate("Research Evidence")}>Model and source limits <ArrowUpRight size={14} /></button></div>
    </aside>
    {menu && <button className="overlay" aria-label="Close navigation backdrop" onClick={() => setMenu(false)} />}
    <div className="content-shell">
      <header className="topbar">
        <button className="mobile-toggle icon-button" aria-label={menu ? "Close navigation" : "Open navigation"} onClick={() => setMenu(!menu)}>
          {menu ? <X size={21} /> : <Menu size={21} />}</button>
        <div className="breadcrumb"><span>Workspace</span><ChevronDown size={13} /><strong>{view}</strong></div>
        <div className="live-chip"><Radio size={15} /><span>Live strategy unavailable</span></div>
      </header>
      <main id="main">
        <div className="context-bar">
          <label className="field scenario"><span><Flag size={13} />Research preset</span>
            <select value={context.preset} onChange={e => select(e.target.value as PresetId)} aria-label="Research preset">
              {!preset && <option value={context.preset}>Loading presets…</option>}
              {presets.map(p => <option key={p.id} value={p.id}>{p.title}</option>)}</select></label>
          <label className="field"><span>Driver</span><select aria-label="Driver" value={context.driver} disabled={timing}
            onChange={e => change({ ...context, driver: Number(e.target.value) as 63 | 4 })}>
            {(snapshot?.driver_options ?? [{ id: 63, code: "RUS" }, { id: 4, code: "NOR" }]).map(d =>
              <option key={d.id} value={d.id}>#{d.id} · {d.code}</option>)}
          </select></label>
          <label className="field"><span>Horizon</span><select aria-label="Horizon" value={context.horizon} disabled={timing}
            onChange={e => change({ ...context, horizon: Number(e.target.value) as Context["horizon"] })}>
            {[3, 5, 10].map(n => <option key={n} value={n}>{n} laps</option>)}</select></label>
          <div className="context-actions"><button className="secondary" aria-expanded={browse} onClick={() => setBrowse(!browse)}>Browse presets <ChevronDown size={15} /></button></div>
        </div>
        {browse && <section className="preset-picker" aria-label="Available research presets">
          {presets.map(p => <button key={p.id} onClick={() => select(p.id)} className={p.id === context.preset ? "chosen" : ""}>
            <strong>{p.title}</strong><span>{p.summary}</span><small>{readable(p.mode)}</small></button>)}</section>}
        {snapshot && <details className="workspace-details">
          <summary>Assumptions and context</summary>
          <form key={`${contextKey}/${editorVersion}`} onSubmit={applyAssumptions} onChange={() => setEditingContext(true)}>
            <div className="form-row">
              <label className="field">Research mode<select name="mode" defaultValue={snapshot.context.mode ?? undefined}>
                {snapshot.preset.mode !== "SHADOW_OBSERVATION" &&
                  <option value={snapshot.preset.mode}>{readable(snapshot.preset.mode)}</option>}
                {snapshot.preset.mode === "DECLARED_COUNTERFACTUAL" && <option value="HINDSIGHT_ANALYSIS">Retrospective analysis</option>}
                {snapshot.preset.mode === "SHADOW_OBSERVATION" ? <option value="SHADOW_OBSERVATION">Shadow observation</option> :
                  <option value="LIVE" disabled>Live unavailable</option>}
              </select></label>
              <label className="field">Regulation era<select name="era" defaultValue={context.model_era ?? "2023_2025"}>
                <option value="2023_2025">2023–2025</option><option value="2026">2026 support check</option></select></label>
              <label className="field">Cutoff (UTC)<input name="cutoff" placeholder={snapshot.context.cutoff ?? "ISO 8601 UTC"}
                defaultValue={context.cutoff ?? ""} /></label>
              <label className="field">Session ID<input name="session" type="number" min="1" max="99999"
                placeholder={String(snapshot.context.session ?? "")} defaultValue={context.session ?? ""} /></label>
            </div>
            <div className="form-row assumptions">
              <label className="field">Initial gap (m)<input name="gap" type="number" min=".001" max="690" step="any" defaultValue={contextOptions.gap_m ?? 24.635} /></label>
              <label className="field">Pace offset (s)<input name="pace" type="number" min="-1" max="1" step="any" defaultValue={contextOptions.pace_offset_s ?? 0} /></label>
              <label className="field">Service (s)<input name="service" type="number" min="1" max="10" step="any" defaultValue={contextOptions.service_s ?? 3} /></label>
              <label className="field">Extra pit travel (s)<input name="pit" type="number" min="0" max="10" step="any" defaultValue={contextOptions.extra_pit_travel_s ?? 0} /></label>
              <label className="field">VSC start (s)<input name="vscAt" type="number" min="1" max="240" defaultValue={contextOptions.vsc_at_s ?? 5} /></label>
              <label className="field">VSC duration (s)<input name="vscWindow" type="number" min="20" max="240" defaultValue={contextOptions.vsc_duration_s ?? 75} /></label>
              <label className="field">SC start (s)<input name="scAt" type="number" min="1" max="240" defaultValue={contextOptions.sc_at_s ?? 5} /></label>
              <label className="field">SC duration (s)<input name="scWindow" type="number" min="20" max="240" defaultValue={contextOptions.sc_duration_s ?? 92} /></label>
            </div>
            <button className="secondary" type="submit">Apply context</button>
            {editingContext && <button className="text-button discard" type="button"
              onClick={() => { setEditingContext(false); setEditorVersion(n => n + 1); }}>Discard edits</button>}
          </form>
        </details>}
        {view !== "Overview" && <div className="page-heading"><div className="eyebrow"><Gauge size={14} />Research workspace</div>
          <h1>{view}</h1><p>{descriptor?.description}</p></div>}
        {snapshotError || catalogError ? <div className="flow">
          <Notice bad>{snapshotError || catalogError}</Notice>
          <button className="secondary" onClick={() => { setCatalogError(""); setLoaded(undefined); setGeneration(n => n + 1); }}>Try again</button>
          {snapshotError && <button className="secondary" onClick={() => change({ preset: context.preset, driver: context.driver, horizon: context.horizon })}>Reset context</button>}
        </div> : !snapshot ? <Empty title="Loading the research snapshot" loading /> :
          <div className="flow" key={snapshot.snapshot_id}>
            {snapshot.support.includes("MODEL_ERA_UNSUPPORTED") && <Notice>2026 regulation transfer is unsupported. <code>MODEL_ERA_UNSUPPORTED</code></Notice>}
            {snapshot.support.some(s => s.endsWith("NOT_IN_VERIFIED_CATALOG")) &&
              <Notice>The requested cutoff or session is outside the verified catalog.</Notice>}
            {editingContext ? <Notice>Apply or discard the draft context before viewing results for this workspace.</Notice> : <>
            {reopened && reopened.snapshot_id === snapshot.snapshot_id && <Panel title={`Reopened · ${reopened.title}`}
              subtitle="Model, sources and snapshot identity verified">
              {reopened.receipt && reopened.analysis ? (
                chosen?.result?.result_sha256 === reopened.receipt.result_sha256 ?
                  <p role="status" className="verified">Saved result checksum verified · {reopened.receipt.result_sha256.slice(0, 16)}</p> :
                  <div><button className="primary" disabled={Boolean(chosen && active(chosen))} onClick={() => {
                    if (reopened.analysis && reopened.receipt) void start(reopened.analysis.operation, "/replays", {
                      request: reopened.analysis, expected_sha256: reopened.receipt.result_sha256,
                    });
                  }}>Reproduce saved analysis</button>
                  {chosen && !active(chosen) && chosen.state !== "COMPLETED" && <JobFeedback job={chosen} cancel={() => op && cancel(op)} />}
                  </div>
              ) : <p className="muted">Saved inputs are ready to submit.</p>}
            </Panel>}
            {view === "Overview" && <Overview snapshot={snapshot} forecast={jobs.forecast}
              comparison={jobs.robust?.result ? jobs.robust : jobs.manual} navigate={navigate} chooseDeclared={declared} />}
            {view === "Race State" && <RaceState snapshot={snapshot} chooseDeclared={declared} />}
            {view === "Pace Forecast" && <ForecastView snapshot={snapshot} {...jobProps} />}
            {view === "Strategy Lab" && <StrategyView snapshot={snapshot} {...jobProps} operation={comparison}
              reference={reference} setReference={setReference} chooseTeam={() => select("shared-box", "Strategy Lab")}
              chooseDeclared={declared} selectOperation={op => { setComparison(op); setLastOperation(op); }} />}
            {view === "Robust Strategy" && <RobustView snapshot={snapshot} {...jobProps} chooseDeclared={() => select("neutralization", "Robust Strategy")} />}
            {view === "Timing Risk" && <TimingView snapshot={snapshot} {...jobProps} choose={() => select("timing", "Timing Risk")} />}
            {view === "Research Evidence" && <ResearchEvidence snapshot={snapshot} />}
            {view === "Workspaces" && <WorkspaceShelf build={buildWorkspace} reopen={reopen} available={!editingContext} />}
            {chosen?.result && <div className="actions"><button className="secondary" onClick={() => navigate("Workspaces")}>
              <FolderOpen size={15} /> Save this investigation</button></div>}
            {chosen?.result && <Reproducibility job={chosen} replay={verify}
              replayJob={jobs.replay?.result && jobs.replay.result.analysis_id !== chosen.result.analysis_id ? undefined : jobs.replay}
              cancelReplay={() => cancel("replay")} />}
            {previous && ["Strategy Lab", "Robust Strategy", "Workspaces", "Pace Forecast"].includes(view) &&
              previous.result_sha256 !== chosen?.result?.result_sha256 && <Panel title="Previous analysis" subtitle="Earlier inputs · retained for reference">
                <details><summary>Inspect previous result identity</summary>
                  <DataTable headings={["Preset", "Driver", "Horizon", "Completion", "Result checksum"]}
                    rows={[[previous.request.context.preset, previous.request.context.driver, previous.request.context.horizon,
                      readable(previous.completion), <code key="h">{previous.result_sha256}</code>]]} />
                  {previous.payload.decision?.rows && <DataTable headings={["Previous policy", "Effective time (s)"]}
                    rows={previous.payload.decision.rows.map(row => [row.candidate, seconds(row.objective_us, 6)])} />}
                </details>
              </Panel>}
            </>}
          </div>}
        <footer><ShieldCheck size={14} /> Private research only <span>·</span> No live strategy or operational race probabilities</footer>
      </main>
    </div>
  </div>;
}
