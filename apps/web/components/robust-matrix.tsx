"use client";

import { useState } from "react";
import type { Result } from "../lib/api";
import { AuditTable, DataTable, Notice, Panel, policyName, readable, seconds } from "./ui";

export function RobustMatrix({ result }: { result: Result }) {
  const data = result.payload;
  const rows = data.analysis?.rows ?? [];
  const futures = Object.keys(data.cases ?? {});
  const [pick, setPick] = useState("");
  const [other, setOther] = useState("");
  const [future, setFuture] = useState("");
  const first = pick || result.recommendation.alternatives[0] || rows[0]?.policy;
  const second = other || rows.find(r => r.policy !== first)?.policy || first;
  const rank = [...rows].filter(row => row.worst_regret_us != null).sort((a, b) => a.worst_regret_us! - b.worst_regret_us!);
  const peak = Math.max(1, ...rank.map(r => r.worst_regret_us!));
  const lookup = (policy: string, scenario: string) => data.analysis?.regret[policy]?.[scenario];
  const declared = first ? futures.filter(f => lookup(first, f) != null) : [];
  const worst = declared.reduce((best, item) => (lookup(first, item) ?? -Infinity) > (lookup(first, best) ?? -Infinity) ? item : best, "");
  const scenario = future || worst || futures[0];
  const pareto = rows.filter(r => r.comparable && !r.dominated_by.length);
  return <>
    <Panel title="Regret heatmap" className="matrix-panel" subtitle={`Executable policies · ${data.comparator ?? "Comparator unavailable"} · seconds`}>
      <DataTable headings={["Policy", ...futures, "Worst regret (s)"]}
        rows={rows.map(r => [policyName(r.policy), ...futures.map(f => {
          const value = lookup(r.policy, f);
          return value == null ? "—" : <button key={f} className="heat-cell" type="button"
            aria-label={`${policyName(r.policy)} ${f} regret ${seconds(value, 6)} seconds`}
            style={{ backgroundColor: `rgba(238,117,70,${.10 + .44 * Math.min(1, value / peak)})` }}
            onClick={() => { setPick(r.policy); setFuture(f); }}>{seconds(value, 3)}</button>;
        }), seconds(r.worst_regret_us, 3)])} />
      {!rank.length && <Notice>The evaluated subset cannot support a complete worst-case ranking.</Notice>}
      {rank.length > 0 && <details><summary>Worst-case regret ranking</summary>
        <div className="regret-ranking">{rank.map(r => <button className="regret-rank" key={r.policy} onClick={() => {
          setPick(r.policy); setFuture("");
        }}><span>{policyName(r.policy)}</span><i style={{ width: `${100 * r.worst_regret_us! / peak}%` }} />
          <b>{seconds(r.worst_regret_us)} s</b></button>)}</div></details>}
    </Panel>
    <Panel title="Selected-policy comparison" subtitle="Each selected policy uses the same executable rule across futures">
      <div className="form-row">
        <label className="field">Primary robust policy<select aria-label="Primary robust policy" value={first ?? ""} onChange={e => { setPick(e.target.value); setFuture(""); }}>
          {rows.map(r => <option key={r.policy} value={r.policy}>{policyName(r.policy)}</option>)}</select></label>
        <label className="field">Comparator robust policy<select aria-label="Comparator robust policy" value={second ?? ""} onChange={e => setOther(e.target.value)}>
          {rows.map(r => <option key={r.policy} value={r.policy}>{policyName(r.policy)}</option>)}</select></label>
        <label className="field">Inspect future<select aria-label="Inspect future" value={scenario ?? ""} onChange={e => setFuture(e.target.value)}>
          {futures.map(f => <option key={f}>{f}</option>)}</select></label>
      </div>
      <p className="muted">{worst ? `Worst evaluated future for the primary policy · ${worst}` : "Worst evaluated future unavailable."}</p>
      <DataTable headings={["Future", `${policyName(first || "—")} elapsed (s)`, `${policyName(second || "—")} elapsed (s)`, "Best evaluated"]}
        rows={futures.map(f => [f, seconds(data.cases?.[f]?.rows[first]?.objective_us, 6),
          seconds(data.cases?.[f]?.rows[second]?.objective_us, 6), data.sensitivity?.[f]?.map(policyName).join(" / ") || "—"])} />
      <h3>Executed actions · {scenario}</h3>
      {[...new Set([first, second])].filter(Boolean).map(policy => {
        const row = data.cases?.[scenario]?.rows[policy];
        return <div key={policy}><p><strong>{policyName(policy)}</strong> · {row ? readable(row.status) : "Unevaluated"}</p>
          <DataTable headings={["Driver", "Executed lap", "Set", "Entry (s)", "Rejoin (s)"]}
            rows={(row?.visited_pits ?? []).map(p => [p.driver ?? "—", p.target_lap ?? "—", p.set_id ?? "—",
              seconds(p.entry_us, 6), seconds(p.merge_us, 6)])} />
          {row?.committed_actions && row.committed_actions.length > 0 && <details><summary>Engine action records</summary>
            <pre className="ledger-json">{JSON.stringify(row.committed_actions, null, 2)}</pre></details>}
        </div>;
      })}
      <details><summary>Declared control scripts</summary>{futures.map(f => <div key={f}><h3>{f}</h3><AuditTable rows={data.scenario_scripts?.[f] ?? []} /></div>)}</details>
    </Panel>
    <Panel title="Pareto alternatives" subtitle="Comparable policies without an evaluated dominator">
      {result.completion !== "ROBUST_SEARCH_COMPLETE" && <Notice>These classifications use the evaluated subset.</Notice>}
      <div className="chips">{pareto.map(row => <button key={row.policy} onClick={() => setPick(row.policy)}>{policyName(row.policy)}</button>)}</div>
      {!pareto.length && <p className="muted">Comparable Pareto alternatives are unavailable.</p>}
    </Panel>
  </>;
}
