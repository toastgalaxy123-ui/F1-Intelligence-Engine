"use client";

import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { BookOpen, Copy, Download, Save, Trash2, Upload } from "lucide-react";
import { post } from "../lib/api";
import { duplicateWorkspace, readShelf, readWorkspace, storageKey, writeShelf,
  maximumWorkspaceBytes, type WorkspaceFile, type WorkspaceVerification } from "../lib/workspaces";
import { Empty, Notice, Panel, Status } from "./ui";

export function WorkspaceShelf({ build, reopen, available }: {
  build: (title: string) => WorkspaceFile;
  reopen: (doc: WorkspaceFile) => void;
  available: boolean;
}) {
  const [rows, setRows] = useState<WorkspaceFile[]>([]);
  const [title, setTitle] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState("");
  const file = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function refresh() {
      try { setRows(readShelf(window.localStorage)); }
      catch (cause) { setError(cause instanceof Error ? cause.message : "The local shelf is unavailable."); }
    }
    const timer = setTimeout(refresh, 0);
    function external(event: StorageEvent) { if (event.key === storageKey) refresh(); }
    window.addEventListener("storage", external);
    return () => { clearTimeout(timer); window.removeEventListener("storage", external); };
  }, []);

  function update(transform: (items: WorkspaceFile[]) => WorkspaceFile[]) {
    const next = transform(readShelf(window.localStorage));
    writeShelf(window.localStorage, next);
    setRows(next);
  }

  async function verify(doc: WorkspaceFile) {
    const result = await post<WorkspaceVerification>("/workspaces/verify", doc);
    if (result.status !== "INPUTS_VERIFIED" || result.snapshot_id !== doc.snapshot_id ||
        (doc.receipt && result.analysis_id !== doc.receipt.analysis_id)) {
      throw new Error("The saved workspace could not be verified.");
    }
  }

  async function action(run: () => Promise<void>) {
    setPending(true); setError(""); setMessage("");
    try { await run(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "The workspace operation failed."); }
    finally { setPending(false); }
  }

  function importFile(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const selected = input.files?.[0];
    input.value = "";
    if (!selected) return;
    void action(async () => {
      if (selected.size > maximumWorkspaceBytes) throw new Error("The workspace exceeds 15 KiB.");
      const doc = readWorkspace(await selected.text());
      await verify(doc);
      update(items => {
        if (items.some(item => item.id === doc.id)) throw new Error("This workspace is already on the shelf.");
        return [doc, ...items];
      });
      setMessage("Verified workspace imported.");
    });
  }

  function download(doc: WorkspaceFile) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `f1ri-workspace-${doc.id}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return <Panel title="Saved research workspaces" subtitle="Stored in this browser · export JSON to keep a backup">
    <form className="form-row" onSubmit={event => {
      event.preventDefault();
      void action(async () => {
        const doc = build(title.trim());
        await verify(doc);
        update(items => [doc, ...items]);
        setTitle(""); setMessage("Workspace saved.");
      });
    }}>
      <label className="field">Workspace title<input maxLength={80} value={title} onChange={e => setTitle(e.target.value)}
        placeholder="Name this investigation" /></label>
      <button className="primary" disabled={!available || pending}><Save size={15} /> Save current workspace</button>
      <button type="button" className="secondary" disabled={pending} onClick={() => file.current?.click()}><Upload size={15} /> Import workspace</button>
      <input ref={file} type="file" accept=".json,application/json" hidden aria-label="Import workspace file" onChange={importFile} />
    </form>
    {error && <Notice bad>{error}</Notice>}
    {message && <p role="status" className="verified">{message}</p>}
    {pending && <p role="status" className="muted">Checking the workspace inputs…</p>}
    {!rows.length && !error && <Empty title="No saved workspaces" detail="Save the selected context and its current analysis to return to it later." />}
    <div className="shelf">{rows.map(doc => <article className="shelf-item" key={doc.id}>
      <div><h3>{doc.title}</h3><p>#{doc.context.driver} · {doc.context.horizon} laps · {doc.context.preset}
        {doc.analysis ? ` · ${doc.analysis.operation}` : ""}</p><Status tone={doc.receipt ? "good" : "neutral"}>
          {doc.receipt ? "Replay receipt" : "Saved inputs"}</Status></div>
      <div className="actions">
        <button className="secondary" disabled={pending} onClick={() => void action(async () => { await verify(doc); reopen(doc); })}>
          <BookOpen size={14} /> Reopen <span className="sr-only">{doc.title}</span></button>
        <button className="secondary" disabled={pending} onClick={() => void action(async () => {
          update(items => [duplicateWorkspace(doc, crypto.randomUUID(), new Date().toISOString()), ...items]);
          setMessage("Workspace duplicated.");
        })}><Copy size={14} /> Duplicate <span className="sr-only">{doc.title}</span></button>
        <button className="secondary" disabled={pending} onClick={() => download(doc)}><Download size={14} /> Export <span className="sr-only">{doc.title}</span></button>
        {confirm === doc.id ? <>
          <button className="secondary" onClick={() => void action(async () => {
            update(items => items.filter(item => item.id !== doc.id)); setConfirm(""); setMessage("Workspace deleted.");
          })}>Confirm delete <span className="sr-only">{doc.title}</span></button>
          <button className="secondary" onClick={() => setConfirm("")}>Keep workspace</button>
        </> : <button className="secondary" disabled={pending} onClick={() => setConfirm(doc.id)}>
          <Trash2 size={14} /> Delete <span className="sr-only">{doc.title}</span></button>}
      </div>
    </article>)}</div>
  </Panel>;
}
