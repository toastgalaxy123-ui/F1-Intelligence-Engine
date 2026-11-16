import test from "node:test";
import assert from "node:assert/strict";
import { duplicateWorkspace, readShelf, readWorkspace, storageKey, writeShelf } from "../lib/workspaces.ts";

const creation = "2026-11-16T02:00:00.000Z";
const key = "aa".repeat(32);
const base = {
  version:"s8b-workspace-v1", id:"3155dbba-4255-4bc2-8f6f-d79022781857", title:"Slipstream lab",
  created_at:creation, updated_at:creation, context:{preset:"two-car",driver:63,horizon:5},
  snapshot_id:key, model_sha256:key, source_hashes:{approved:key}, support:[], analysis:null,
  receipt:null, reference:"reference",
};
test("valid format round-trips through bounded storage", () => {
  const entries = new Map();
  const store = { getItem:k=>entries.get(k), setItem:(k,v)=>entries.set(k,v) };
  assert.deepEqual(readShelf(store), []);
  writeShelf(store,[base]);
  assert.deepEqual(readShelf(store), [base]);
  assert.ok(entries.has(storageKey));
  const copy = duplicateWorkspace(base,"28062762-bd60-449e-b7a0-dbd1f460a581",creation);
  assert.notEqual(copy.id,base.id);
  assert.equal(copy.title,"Slipstream lab (copy)");
});
test("rejects unsupported, oversized or incoherent workspace documents and shelf", () => {
  assert.throws(()=>readWorkspace(JSON.stringify({...base,version:"unrecognized"})), /format/);
  assert.throws(()=>readWorkspace(JSON.stringify({...base,command:"ignored"})), /format/);
  assert.throws(()=>readWorkspace(JSON.stringify({...base,receipt:{analysis_id:key,result_sha256:key}})), /format/);
  assert.throws(()=>readWorkspace("x".repeat(16*1024)), /15 KiB/);
  assert.throws(()=>writeShelf({setItem(){}},Array.from({length:25},()=>base)), /24 workspaces/);
});
