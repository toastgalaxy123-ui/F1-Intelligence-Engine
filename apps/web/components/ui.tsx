"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, ArrowUpRight, CircleDot, Flag, LoaderCircle } from "lucide-react";
import type { AuditRow, Forecast, Policy, Sourced } from "../lib/api";

export const palette = ["#f57d4d", "#68b9fc", "#e7ba59", "#9bcf9f", "#c294ef"];
const names: Record<string, string> = {
  HISTORICAL_CUTOFF_REPLAY: "Historical cutoff", DECLARED_COUNTERFACTUAL: "Declared counterfactual",
  HINDSIGHT_ANALYSIS: "Retrospective analysis", SHADOW_OBSERVATION: "Shadow observation",
  LIVE_UNSUPPORTED: "Live strategy unavailable", MEAN_ONLY_VALIDATED: "Mean only",
  NO_UNIQUE_RECOMMENDATION: "No unique supported recommendation",
  CONSTRAINT_DOMINATED_IDENTIFIABILITY: "Following constraints make different pace forecasts produce the same effective result.",
  HISTORICAL_STRATEGY_UNSUPPORTED: "Historical strategy is unavailable at this cutoff.",
  FULL_FIELD_PHYSICAL_STATE_UNAVAILABLE: "Historical physical field unavailable",
  HISTORICAL_INVENTORY_UNAVAILABLE: "Historical tire inventory unavailable",
  MODEL_ERA_UNSUPPORTED: "2026 regulation transfer is unsupported.",
  BOUNDED_SEARCH_INCOMPLETE: "Search budget exhausted", ROBUST_SEARCH_COMPLETE: "Robust search complete",
  EXHAUSTED_LEGAL_SPACE: "Legal search space exhausted", ARCHIVED_MEAN_ONLY: "Archived mean pace only",
  EXACT_SIMULATION_TIE: "Exact simulation tie", NEAR_EQUAL_ALTERNATIVES: "Near-equal alternatives",
  FAMILY_SENSITIVE: "Ranking varies across declared futures",
  SHADOW_OBSERVATION_ONLY: "Read-only shadow observation", MANUAL_POLICY_SET_EVALUATED: "Selected policies evaluated",
  FROZEN_TIMING_REPRODUCED: "Frozen timing result reproduced",
};

export const readable = (key: string) => names[key] ?? key.toLowerCase().split("_").join(" ").replace(/^./, c => c.toUpperCase());
export const unavailable = "Unavailable";
export const precision = (value: number | null | undefined, decimals = 3) => value == null ? "—" : value.toFixed(decimals);
export const seconds = (us: number | null | undefined, decimals = 3) => precision(us == null ? null : us / 1_000_000, decimals);
export const valueOf = <T,>(field: Sourced<T> | undefined) => field?.value ?? null;

export function policyName(id: string) {
  if (id.includes("reference") || id === "stay-out" || id === "p0-stay-out") return "Stay out";
  const stop = id.match(/^\d+-L(\d+)-(.+)$/);
  if (stop) return `Lap ${stop[1]} · ${stop[2]}`;
  return id.replaceAll("-", " ");
}
export function optionLabel(policy: Policy) {
  if (!policy.stops.length) return "Stay out · reference";
  return policy.stops.map(s => `${s.lap == null ? readable(policy.kind) : "Lap " + s.lap} · ${s.set_id}`)
    .join(" + ") + ` · #${policy.stops[0].driver}`;
}

export function Compound({ name, compact = false }: { name: string | undefined | null; compact?: boolean }) {
  if (!name) return <span className="muted">{unavailable}</span>;
  const label = name.replace("used-", "").toUpperCase();
  return <span className="compound"><span className={`tyre ${label.toLowerCase()}`}>{label[0]}</span>{!compact && label}</span>;
}

export function Status({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "warm" | "good" | "bad" }) {
  return <span className={`status ${tone}`}><CircleDot size={12} />{children}</span>;
}

export function Panel({ title, subtitle, action, children, className = "" }:
  { title: string; subtitle?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>
    <div className="panel-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>
    {children}
  </section>;
}

export function LinkButton({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return <button className="text-button" onClick={onClick}>{children}<ArrowUpRight size={15} /></button>;
}

export function Empty({ title, detail, loading = false }: { title: string; detail?: string; loading?: boolean }) {
  return <div className="empty" role={loading ? "status" : undefined}>
    {loading ? <LoaderCircle className="spin" size={22} /> : <Flag size={20} />}
    <strong>{title}</strong>{detail && <p>{detail}</p>}
  </div>;
}
export function Notice({ children, bad = false }: { children: ReactNode; bad?: boolean }) {
  return <div className={`notice${bad ? " error" : ""}`} role={bad ? "alert" : undefined}>
    <AlertTriangle size={17} /><div>{children}</div>
  </div>;
}
export function DataTable({ headings, rows, caption }: { headings: string[]; rows: ReactNode[][]; caption?: string }) {
  if (!rows.length) return <p className="muted table-empty">{unavailable}</p>;
  return <div className="table-scroll"><table>
    {caption && <caption>{caption}</caption>}
    <thead><tr>{headings.map((h, i) => <th key={i} scope="col">{h}</th>)}</tr></thead>
    <tbody>{rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody>
  </table></div>;
}

export function AuditTable({ rows }: { rows: AuditRow[] }) {
  if (!rows.length) return <p className="muted table-empty">No entries in this ledger.</p>;
  const keys = [...new Set(rows.flatMap(Object.keys))];
  return <DataTable headings={keys.map(k => readable(k.replace(/_us$/, "_s")))}
    rows={rows.map(row => keys.map(key => {
      const value = row[key];
      return value == null ? "—" : key.endsWith("_us") && typeof value === "number" ? seconds(value) : String(value);
    }))} />;
}

export function PaceChart({ series, compact = false }: { series: Forecast[]; compact?: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const rows = series.flatMap(s => s.mean);
  const minLap = Math.min(...rows.map(r => r.lap));
  const maxLap = Math.max(...rows.map(r => r.lap));
  useEffect(() => {
    const el = canvas.current;
    if (!el || !rows.length) return;
    const draw = () => {
      const cx = el.getContext("2d");
      if (!cx) return;
      const width = el.clientWidth, height = el.clientHeight, ratio = window.devicePixelRatio || 1;
      el.width = Math.round(width * ratio);
      el.height = Math.round(height * ratio);
      cx.scale(ratio, ratio);
      const min = Math.min(...rows.map(r => r.seconds));
      const max = Math.max(...rows.map(r => r.seconds));
      const step = Math.max(.05, Math.ceil((max - min) * 5) / 20);
      const bottom = Math.floor((min - step * .35) / step) * step;
      const top = Math.max(bottom + step * 4, Math.ceil((max + step * .15) / step) * step);
      const padding = { left: 49, right: 20, top: 17, bottom: 31 };
      const x = (lap: number) => padding.left + (width - padding.left - padding.right) *
        (lap - minLap) / Math.max(1, maxLap - minLap);
      const y = (s: number) => padding.top + (height - padding.top - padding.bottom) *
        (top - s) / (top - bottom);
      cx.font = "11px Geist, sans-serif";
      cx.strokeStyle = "#293039";
      cx.fillStyle = "#8c98a6";
      cx.setLineDash([3, 4]);
      for (let tick = bottom; tick <= top + .00001; tick += step) {
        cx.beginPath(); cx.moveTo(padding.left, y(tick)); cx.lineTo(width - padding.right, y(tick)); cx.stroke();
        cx.fillText(tick.toFixed(2), 5, y(tick) + 4);
      }
      cx.setLineDash([]);
      for (let lap = minLap; lap <= maxLap; lap++) {
        if (!compact || (lap - minLap) % Math.ceil((maxLap - minLap + 1) / 6) === 0 || lap === maxLap) {
          cx.fillText(`L${lap}`, x(lap) - 9, height - 10);
        }
      }
      if (hover !== null) {
        cx.strokeStyle = "#5d6773"; cx.beginPath(); cx.moveTo(x(hover), padding.top);
        cx.lineTo(x(hover), height - padding.bottom); cx.stroke();
      }
      series.forEach((s, index) => {
        const color = palette[index % palette.length];
        if (!s.mean.length) return;
        if (index === 0) {
          const points = s.mean;
          const gradient = cx.createLinearGradient(0, padding.top, 0, height - padding.bottom);
          gradient.addColorStop(0, "#f57d4d47"); gradient.addColorStop(1, "#f57d4d00");
          cx.beginPath(); cx.moveTo(x(points[0].lap), height - padding.bottom);
          points.forEach(row => cx.lineTo(x(row.lap), y(row.seconds)));
          cx.lineTo(x(points[points.length - 1].lap), height - padding.bottom); cx.closePath();
          cx.fillStyle = gradient; cx.fill();
        }
        cx.beginPath();
        s.mean.forEach((row, i) => i ? cx.lineTo(x(row.lap), y(row.seconds)) : cx.moveTo(x(row.lap), y(row.seconds)));
        cx.strokeStyle = color; cx.lineWidth = 2.4; cx.stroke();
        s.mean.forEach(row => {
          cx.beginPath(); cx.arc(x(row.lap), y(row.seconds), hover === row.lap ? 5 : 3.5, 0, 2 * Math.PI);
          cx.fillStyle = "#171c21"; cx.fill(); cx.lineWidth = 1.7; cx.stroke();
        });
      });
    };
    const resize = new ResizeObserver(draw);
    resize.observe(el);
    draw();
    return () => resize.disconnect();
  }, [series, rows, minLap, maxLap, hover, compact]);
  if (!rows.length) return <Empty title="No supported mean trajectory" />;
  return <div className="pace-chart">
    <div className="chart-axis-label">Mean lap time (s)</div>
    <canvas ref={canvas} className={compact ? "compact-chart" : ""} role="img" aria-label="D8R1 mean pace by lap"
      tabIndex={0} onKeyDown={e => {
        if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
        e.preventDefault(); setHover(Math.max(minLap, Math.min(maxLap, (hover ?? minLap) + (e.key === "ArrowLeft" ? -1 : 1))));
      }} onPointerMove={e => {
        const bounds = e.currentTarget.getBoundingClientRect();
        const frac = (e.clientX - bounds.left - 49) / (bounds.width - 69);
        setHover(Math.max(minLap, Math.min(maxLap, Math.round(minLap + frac * (maxLap - minLap)))));
      }} onPointerLeave={() => setHover(null)} />
    <div className="chart-legend" aria-live="polite">
      {series.map((s, i) => <span key={s.plan}><i style={{ background: palette[i % palette.length] }} />
        {s.plan}{hover !== null && s.mean.find(r => r.lap === hover) ?
          ` · L${hover} ${precision(s.mean.find(r => r.lap === hover)?.seconds)} s` : ""}</span>)}
    </div>
  </div>;
}
