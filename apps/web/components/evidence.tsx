"use client";

import { useEffect, useState } from "react";
import { FileCheck2, ShieldCheck } from "lucide-react";
import { api, type Evidence, type Shadow, type ShadowCheckpoint, type Snapshot } from "../lib/api";
import { DataTable, Empty, Notice, Panel, Status, precision, readable } from "./ui";

export function ResearchEvidence({ snapshot }: { snapshot: Snapshot }) {
  const [evidence, setEvidence] = useState<Evidence>();
  const [shadow, setShadow] = useState<Shadow>();
  const [detail, setDetail] = useState<ShadowCheckpoint>();
  const [index, setIndex] = useState(0);
  const [error, setError] = useState("");
  const [sourceOpen, setSourceOpen] = useState(snapshot.context.preset === "shadow");
  useEffect(() => {
    const abort = new AbortController();
    api<Evidence>("/evidence", { signal: abort.signal }).then(setEvidence).catch(e => {
      if (!abort.signal.aborted) setError(e.message);
    });
    return () => abort.abort();
  }, []);
  useEffect(() => {
    if (!sourceOpen) return;
    const abort = new AbortController();
    api<Shadow>("/evidence/shadow", { signal: abort.signal }).then(setShadow).catch(e => {
      if (!abort.signal.aborted) setError(e.message);
    });
    return () => abort.abort();
  }, [sourceOpen]);
  useEffect(() => {
    if (!sourceOpen) return;
    const abort = new AbortController();
    api<ShadowCheckpoint>(`/evidence/shadow/${index}`, { signal: abort.signal }).then(setDetail).catch(e => {
      if (!abort.signal.aborted) setError(e.message);
    });
    return () => abort.abort();
  }, [index, sourceOpen]);
  return <>
    {error && <Notice bad>{error}</Notice>}
    {evidence ? <>
      <div className="overview-grid equal">
        <Panel title="Model release" subtitle={evidence.model.version} action={<Status tone="good">Mean only</Status>}>
          <DataTable headings={["Validation", "Support"]} rows={[
            ["2023–2025 pace mean", "Validated"], ["Individual uncertainty intervals", "G3 calibration failed"],
            ["2026 regulation transfer", readable(evidence.model.transfer_2026)], ["Model hash", <code key="hash">{evidence.model.sha256}</code>],
          ]} />
        </Panel>
        <Panel title="Research release readiness" subtitle="Private local workspace" action={<ShieldCheck size={18} className="muted" />}>
          <div className="decision"><span className="decision-icon"><FileCheck2 size={20} /></span><div>
            <h3>Live strategy unavailable</h3><p>Archived reconstruction, declared research and read-only shadow observation have separate evidence requirements.</p></div></div>
          <div className="chips"><span>T1 deterministic no-pass</span><span>No operational race probabilities</span></div>
          <details><summary>Physical evidence limits</summary><DataTable headings={["Field", "Support"]}
            rows={Object.entries(evidence.physical).map(([k, v]) => [readable(k), v ?? "Unavailable"])} /></details>
        </Panel>
      </div>
      <Panel title="Snapshot and source provenance" subtitle={readable(snapshot.context.mode ?? "")}>
        <div className="provenance-ids"><span>Snapshot <code>{snapshot.snapshot_id}</code></span>
          <span>Event cutoff <code>{snapshot.context.cutoff ?? "Unavailable"}</code></span></div>
        <details><summary>Frozen source hashes</summary><DataTable headings={["Artifact", "SHA-256"]}
          rows={Object.entries({ ...evidence.source_hashes, ...snapshot.source_hashes }).map(([file, hash]) => [file, <code key={file}>{hash}</code>])} /></details>
        <details><summary>Technical support codes</summary><div className="code-list">{snapshot.support.map(code => <code key={code}>{code}</code>)}</div></details>
      </Panel>
      <Panel title="Source and shadow evidence" subtitle="S7-C0 checksummed archive retrieval" action={<Status tone="warm">Archive replay</Status>}>
        <div className="chips">
          <span>{evidence.shadow_metrics.sealed} sealed checkpoints</span>
          <span>{evidence.shadow_metrics.withheld} actions withheld</span>
          <span>{evidence.shadow_metrics.contemporary_racing_windows} contemporary racing windows</span>
        </div>
        {!sourceOpen && <button className="secondary" onClick={() => setSourceOpen(true)}>Inspect sealed checkpoints</button>}
        {sourceOpen && !shadow && <Empty title="Loading source evidence" loading />}
        {shadow && <>
          <Notice>Continuous 2-second location freshness is infeasible at the declared request rate. The collector clock was {shadow.clock.synchronization.toLowerCase()}.</Notice>
          <DataTable headings={["Endpoint", "S7-B max usable gap (s)", "S4 max usable gap (s)"]}
            rows={Object.entries(shadow.baseline.endpoints).map(([name, stats]) =>
              [name, precision(stats.max_usable_interval_s), precision(shadow.capture.endpoints[name]?.max_usable_interval_s)])} />
          <label className="field">Receipt cutoff<select value={index} onChange={e => { setIndex(Number(e.target.value)); setDetail(undefined); }}>
            {shadow.checkpoints.map((c, i) => <option key={c.seal_sha256} value={i}>{c.clock}</option>)}</select></label>
          {detail && <><div className="provenance-ids"><span>Sealed decision <code>{detail.seal_sha256}</code></span>
            <span>Archived event cutoff <code>{detail.source_event_cutoff}</code></span></div>
            <p>{detail.earned_versions} visible source versions</p><div className="code-list">{detail.withheld.map(s => <code key={s}>{s}</code>)}</div>
            <details><summary>Endpoint receipt and driver support</summary><DataTable headings={["Endpoint", "Response age (s)", "Statuses"]}
              rows={Object.entries(detail.endpoints ?? {}).map(([ep, frame]) => [ep, precision(frame.response_age_s as number | null),
                (frame.status as string[]).map(readable).join(" · ")])} /></details></>}
        </>}
      </Panel>
    </> : !error && <Empty title="Loading research evidence" loading />}
  </>;
}
