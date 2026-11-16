"use client";

import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { BookOpen, Copy, Download, Pencil, Save, Trash2, Upload } from "lucide-react";
import { api, apiUrl, post, privateMode } from "../lib/api";
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
  const [localDrafts, setLocalDrafts] = useState<WorkspaceFile[]>([]);
  const [editing, setEditing] = useState<{ id: string; title: string }>();
  const file = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const abort = new AbortController();
    async function refresh() {
      try {
        if (privateMode) {
          const remote = await api<WorkspaceFile[]>("/workspaces", { signal: abort.signal });
          if (abort.signal.aborted) return;
          setRows(remote);
          // Old documents are offered explicitly. Nothing writes or removes these originals.
          setLocalDrafts(readShelf(window.localStorage));
        } else setRows(readShelf(window.localStorage));
      } catch (cause) {
        if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "The workspace shelf is unavailable.");
      }
    }
    const timer = setTimeout(() => void refresh(), 0);
    function external(event: StorageEvent) { if (event.key === storageKey) void refresh(); }
    window.addEventListener("storage", external);
    return () => { abort.abort(); clearTimeout(timer); window.removeEventListener("storage", external); };
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

  async function save(doc: WorkspaceFile) {
    if (privateMode) {
      const saved = await post<WorkspaceFile>("/workspaces", doc);
      setRows(items => [saved, ...items]);
      return;
    }
    await verify(doc);
    update(items => {
      if (items.some(item => item.id === doc.id)) throw new Error("This workspace is already on the shelf.");
      return [doc, ...items];
    });
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
      await save(doc);
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

  return <Panel title="Saved research workspaces" subtitle={privateMode ?
    "Private to your account · available on your signed-in devices" : "Stored in this browser · export JSON to keep a backup"}>
    <form className="form-row" onSubmit={event => {
      event.preventDefault();
      void action(async () => {
        const doc = build(title.trim());
        await save(doc);
        setTitle(""); setMessage("Workspace saved.");
      });
    }}>
      <label className="field">Workspace title<input maxLength={80} value={title} onChange={e => setTitle(e.target.value)}
        placeholder="Name this investigation" /></label>
      <button className="primary" disabled={!available || pending}><Save size={15} /> Save current workspace</button>
      <button type="button" className="secondary" disabled={pending} onClick={() => file.current?.click()}><Upload size={15} /> Import workspace</button>
      <input ref={file} type="file" accept=".json,application/json" hidden aria-label="Import workspace file" onChange={importFile} />
    </form>
    {privateMode && <details><summary>Import previous local workspaces</summary>
      <p>For a workspace saved under another browser address, export its JSON there and use Import workspace here.</p>
      {localDrafts.filter(doc => !rows.some(saved => saved.id === doc.id)).map(doc =>
        <button key={doc.id} className="secondary" disabled={pending} onClick={() => void action(async () => {
          await save(doc);
          setMessage("Local workspace copied to your account. The browser original was preserved.");
        })}>Copy {doc.title}</button>)}
    </details>}
    {error && <Notice bad>{error}</Notice>}
    {message && <p role="status" className="verified">{message}</p>}
    {pending && <p role="status" className="muted">Checking the workspace inputs…</p>}
    {!rows.length && !error && <Empty title="No saved workspaces" detail="Save the selected context and its current analysis to return to it later." />}
    <div className="shelf">{rows.map(doc => <article className="shelf-item" key={doc.id}>
      <div><h3>{doc.title}</h3><p>#{doc.context.driver} · {doc.context.horizon} laps · {doc.context.preset}
        {doc.analysis ? ` · ${doc.analysis.operation}` : ""}</p><Status tone={doc.receipt ? "good" : "neutral"}>
          {doc.receipt ? "Replay receipt" : "Saved inputs"}</Status></div>
      <div className="actions">
        <button className="secondary" disabled={pending} onClick={() => void action(async () => {
          const stored = privateMode ? await api<WorkspaceFile>(`/workspaces/${doc.id}`) : doc;
          await verify(stored); reopen(stored);
        })}>
          <BookOpen size={14} /> Reopen <span className="sr-only">{doc.title}</span></button>
        <button className="secondary" disabled={pending} onClick={() => void action(async () => {
          if (privateMode) {
            const copied = await post<WorkspaceFile>(`/workspaces/${doc.id}/duplicate`, {});
            setRows(items => [copied, ...items]);
          } else update(items => [duplicateWorkspace(doc, crypto.randomUUID(), new Date().toISOString()), ...items]);
          setMessage("Workspace duplicated.");
        })}><Copy size={14} /> Duplicate <span className="sr-only">{doc.title}</span></button>
        {privateMode ? <>
          <a className="secondary" href={apiUrl(`/workspaces/${doc.id}/export`)}><Download size={14} /> Export <span className="sr-only">{doc.title}</span></a>
          <button className="secondary" disabled={pending} onClick={() => setEditing({ id: doc.id, title: doc.title })}>
            <Pencil size={14} /> Rename <span className="sr-only">{doc.title}</span></button>
        </> : <button className="secondary" disabled={pending} onClick={() => download(doc)}><Download size={14} /> Export <span className="sr-only">{doc.title}</span></button>}
        {confirm === doc.id ? <>
          <button className="secondary" onClick={() => void action(async () => {
            if (privateMode) {
              await api(`/workspaces/${doc.id}`, { method: "DELETE" });
              setRows(items => items.filter(item => item.id !== doc.id));
            } else update(items => items.filter(item => item.id !== doc.id));
            setConfirm(""); setMessage("Workspace deleted.");
          })}>Confirm delete <span className="sr-only">{doc.title}</span></button>
          <button className="secondary" onClick={() => setConfirm("")}>Keep workspace</button>
        </> : <button className="secondary" disabled={pending} onClick={() => setConfirm(doc.id)}>
          <Trash2 size={14} /> Delete <span className="sr-only">{doc.title}</span></button>}
      </div>
      {editing?.id === doc.id && <form className="form-row" onSubmit={event => {
        event.preventDefault();
        const newTitle = editing.title.trim();
        if (!newTitle) return;
        void action(async () => {
          const value = await api<WorkspaceFile>(`/workspaces/${doc.id}`, {
            method: "PUT", body: JSON.stringify({ ...doc, title: newTitle }),
          });
          setRows(items => items.map(item => item.id === doc.id ? value : item));
          setEditing(undefined); setMessage("Workspace renamed.");
        });
      }}>
        <label className="field">New workspace title<input value={editing.title} maxLength={80}
          onChange={e => setEditing({ id: doc.id, title: e.target.value })} /></label>
        <button className="secondary" disabled={pending}>Save title</button>
        <button className="text-button" type="button" onClick={() => setEditing(undefined)}>Discard rename</button>
      </form>}
    </article>)}</div>
  </Panel>;
}
