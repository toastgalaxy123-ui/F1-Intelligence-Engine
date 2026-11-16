import type { AnalysisRequest, Context } from "./api";

export const workspaceVersion = "s8b-workspace-v1";
export const maximumWorkspaces = 24;
export const maximumWorkspaceBytes = 15 * 1024;
export const storageKey = "f1ri.workspaces.v1";
const sha = /^[0-9a-f]{64}$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const allowed = new Set(["version", "id", "title", "created_at", "updated_at", "context", "snapshot_id",
  "model_sha256", "source_hashes", "support", "analysis", "receipt", "reference"]);

export type WorkspaceFile = {
  version: typeof workspaceVersion; id: string; title: string; created_at: string; updated_at: string;
  context: Context; snapshot_id: string; model_sha256: string; source_hashes: Record<string, string>;
  support: string[]; analysis: AnalysisRequest | null; receipt: { analysis_id: string; result_sha256: string } | null;
  reference: string;
};
export type WorkspaceVerification = {
  status: "INPUTS_VERIFIED"; snapshot_id: string; analysis_id: string | null; replay_available: boolean;
};

export function readWorkspace(raw: string): WorkspaceFile {
  if (new TextEncoder().encode(raw).length > maximumWorkspaceBytes) throw new Error("The workspace exceeds 15 KiB.");
  const value: unknown = JSON.parse(raw);
  const doc = value as Partial<WorkspaceFile> | null;
  if (!doc || typeof doc !== "object" || Object.keys(doc).some(key => !allowed.has(key)) ||
      doc.version !== workspaceVersion || typeof doc.id !== "string" || !uuid.test(doc.id) ||
      typeof doc.title !== "string" || !doc.title.trim() || doc.title.length > 80 ||
      !doc.context || !sha.test(doc.snapshot_id ?? "") || !sha.test(doc.model_sha256 ?? "") ||
      !doc.source_hashes || Array.isArray(doc.source_hashes) ||
      !Array.isArray(doc.support) || typeof doc.reference !== "string" ||
      !doc.created_at || !doc.updated_at || !Number.isFinite(Date.parse(doc.created_at)) ||
      !Number.isFinite(Date.parse(doc.updated_at)) || Date.parse(doc.updated_at) < Date.parse(doc.created_at) ||
      (doc.receipt && (!doc.analysis || !sha.test(doc.receipt.analysis_id) || !sha.test(doc.receipt.result_sha256)))) {
    throw new Error("This file does not match the supported workspace format.");
  }
  return doc as WorkspaceFile;
}

export function readShelf(store: Pick<Storage, "getItem">) {
  const raw = store.getItem(storageKey);
  if (!raw) return [];
  if (raw.length > maximumWorkspaces * maximumWorkspaceBytes) throw new Error("The local workspace shelf exceeds its storage bound.");
  const rows: unknown = JSON.parse(raw);
  if (!Array.isArray(rows) || rows.length > maximumWorkspaces) throw new Error("The local workspace shelf could not be read.");
  return rows.map(row => readWorkspace(JSON.stringify(row)));
}

export function writeShelf(store: Pick<Storage, "setItem">, rows: WorkspaceFile[]) {
  if (rows.length > maximumWorkspaces) throw new Error("The shelf holds up to 24 workspaces. Delete one to make room.");
  rows.forEach(row => readWorkspace(JSON.stringify(row)));
  store.setItem(storageKey, JSON.stringify(rows));
}

export function duplicateWorkspace(workspace: WorkspaceFile, id: string, time: string): WorkspaceFile {
  return { ...workspace, id, created_at: time, updated_at: time, title: `${workspace.title.slice(0, 73)} (copy)` };
}
