import { expect, test, type Page } from "@playwright/test";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { randomUUID, createHash } from "node:crypto";
import { closeSync, openSync } from "node:fs";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { account, api, capture, nav, origin, originHeaders, query, signIn, submit } from "./fixtures";

const stage = process.env.F1RI_STAGE_DIR!;
const caddy = process.env.F1RI_REHEARSAL_CADDY;
const root = resolve("../..");
const challenge = "Restart rehearsal investigation";

function python(script: string, args: string[], input?: string) {
  execFileSync("python", [resolve(root, "scripts", script), ...args], {
    cwd: root, input, stdio: ["pipe", "inherit", "inherit"], timeout: 110_000,
  });
}

async function eventuallyResult(page: Page, job: { id: string }) {
  const path = `${api}/analyses/${job.id}`;
  await expect.poll(async () => (await (await page.request.get(path)).json()).state, { timeout: 38_000 }).toBe("COMPLETED");
  return (await (await page.request.get(path)).json()).result;
}

async function receiptParity(page: Page, output: string) {
  const session = (await page.context().cookies()).find(cookie => cookie.name === "__Host-f1ri-session");
  expect(session).toBeTruthy();
  python("verify_private_parity.py", ["--origin", origin, "--ca", join(stage, "ca.pem"), "--output", output], session!.value);
}

test("H15 H20 — abrupt restart, isolated database restore and compatible build rollback", async ({ browser, request }) => {
  test.skip(!caddy, "Set F1RI_REHEARSAL_CADDY and stop the fixture gateway first.");
  test.setTimeout(360_000);
  expect(origin).toBe("https://localhost:8443");
  const local = await mkdtemp(join(stage, "recovery-"));
  const input = JSON.parse(await readFile(join(stage, "staging.private.json"), "utf8"));
  expect(input.environment).toBe("staging");
  const baseConfig = join(local, "staging.private.json");
  const restoredConfig = join(local, "restored.private.json");
  const backup = join(local, "backup.sqlite3");
  const restored = join(local, "restored.sqlite3");
  const reports = process.env.F1RI_PRIVATE_SCREENSHOTS!;
  expect(reports).toBeTruthy();
  const evidence = resolve(reports, "..");
  await writeFile(baseConfig, JSON.stringify({ ...input, database: join(local, "initial.sqlite3") }), { mode: 0o600 });
  await writeFile(restoredConfig, JSON.stringify({ ...input, database: restored }), { mode: 0o600 });
  const admin = (config: string, params: string[]) => python("private_admin.py", ["--config", config, ...params]);
  admin(baseConfig, ["migrate"]);
  for (const actor of [0, 1] as const) {
    const who = await account(actor);
    admin(baseConfig, ["enroll", "--subject", who.subject, "--label", who.label]);
  }

  let stack: ChildProcess | undefined;
  const pidFile = join(local, "api.pid");
  const start = async (config: string, slot = "") => {
    const log = openSync(join(local, "stack.log"), "a");
    try {
      stack = spawn("python", [resolve(root, "scripts/private_stack.py"), "serve", "--config", config,
        "--caddy", caddy!, "--cert", join(stage, "edge.pem"), "--key", join(stage, "edge.key"),
        "--staging-pid-file", pidFile, ...(slot ? ["--build-slot", slot] : [])], {
        cwd: root, stdio: ["ignore", log, log],
      });
    } finally { closeSync(log); }
    await expect.poll(async () => {
      try { return (await request.get("/healthz", { timeout: 1_200 })).status(); }
      catch { return 0; }
    }, { timeout: 22_000 }).toBe(200);
  };
  const stop = async () => {
    if (!stack || stack.exitCode !== null) return;
    const done = new Promise(resolve => stack!.once("exit", resolve));
    const pid = Number((await readFile(pidFile, "ascii")).trim());
    if (!Number.isSafeInteger(pid) || pid < 2) throw new Error("Expected a staged API PID.");
    process.kill(pid, "SIGKILL");
    await done;
    stack = undefined;
  };
  const login = async (actor: 0 | 1 = 0) => {
    const context = await browser.newContext({ baseURL: origin, ignoreHTTPSErrors: true });
    const page = await context.newPage();
    await signIn(page, actor);
    return page;
  };
  const verifySaved = async (page: Page, hash: string) => {
    await nav(page, "Workspaces");
    await page.getByRole("button", { name: `Reopen ${challenge}`, exact: true }).click();
    await page.getByRole("button", { name: "Reproduce saved analysis", exact: true }).click();
    await expect(page.getByText("Saved result checksum verified", { exact: false })).toBeVisible();
    await expect(page.getByText(hash.slice(0, 12), { exact: false }).first()).toBeVisible();
  };

  try {
    await start(baseConfig);
    let first = await login();
    await receiptParity(first, join(evidence, "api_rehearsal_first.json"));
    await first.context().close();
    first = await login();
    const snap = await (await first.request.get(`${api}/presets/two-car/snapshot`)).json();
    const manual = await query(first.request, "two-car", "manual", { policies: ["63-L31-hard", "63-L31-soft"] });
    const result = await eventuallyResult(first, await submit(first.request, manual));
    const hash = result.result_sha256;
    const stamp = new Date().toISOString();
    const document = {
      version: "s8b-workspace-v1", id: randomUUID(), title: challenge, created_at: stamp, updated_at: stamp,
      context: snap.context, snapshot_id: snap.snapshot_id, model_sha256: result.model.sha256,
      source_hashes: snap.source_hashes, support: snap.support, analysis: result.request,
      receipt: { analysis_id: result.analysis_id, result_sha256: hash },
    };
    expect((await first.request.post(`${api}/workspaces`, { headers: originHeaders, data: document })).status()).toBe(201);

    const work = await query(first.request, "neutralization", "robust", { budget: 128, robust_budget: 236 });
    const draft = { ...document, id: randomUUID(), title: "Pending analysis draft", context: work.context,
      snapshot_id: work.snapshot_id, source_hashes: (await (await first.request.get(`${api}/presets/neutralization/snapshot`)).json()).source_hashes,
      analysis: work, receipt: null };
    expect((await first.request.post(`${api}/workspaces`, { headers: originHeaders, data: draft })).status()).toBe(201);
    const active = await submit(first.request, work);
    await expect.poll(async () => (await (await first.request.get(`${api}/analyses/${active.id}`)).json()).state,
      { intervals: [50, 100, 200], timeout: 8_000 }).toBe("RUNNING");
    const queued = await submit(first.request, { ...work, robust_budget: 235 });
    expect(queued.state).toBe("QUEUED");
    await stop();
    await first.context().close();

    await start(baseConfig);
    const restarted = await login();
    for (const job of [active, queued]) {
      const term = await (await restarted.request.get(`${api}/analyses/${job.id}`)).json();
      expect({ state: term.state, code: term.code, result: term.result }).toEqual({
        state: "FAILED", code: "SERVICE_RESTART", result: null,
      });
      expect((await restarted.request.get(`${api}/analyses/${job.id}/export`)).status()).toBe(409);
    }
    expect((await (await restarted.request.get(`${api}/workspaces/${draft.id}`)).json()).receipt).toBeNull();
    await verifySaved(restarted, hash);
    await capture(restarted, "15-workspace-after-service-restart");
    admin(baseConfig, ["integrity"]);
    admin(baseConfig, ["backup", "--destination", backup]);
    admin(baseConfig, ["restore", "--source", backup, "--destination", restored]);
    admin(restoredConfig, ["integrity"]);
    const cookie = (await restarted.context().cookies()).find(c => c.name === "__Host-f1ri-session")!;
    await stop();
    await restarted.context().close();

    await start(restoredConfig, "candidate");
    expect((await request.get("/auth/session", { headers: { Cookie: `${cookie.name}=${cookie.value}` } })).status()).toBe(401);
    const candidate = await login(1);
    await receiptParity(candidate, join(evidence, "api_rehearsal_candidate.json"));
    await candidate.context().close();
    const restoredPage = await login();
    await verifySaved(restoredPage, hash);
    await capture(restoredPage, "16-workspace-on-restored-database");
    await restoredPage.context().close();
    await stop();

    await start(restoredConfig);
    const rollback = await login();
    await verifySaved(rollback, hash);
    await capture(rollback, "17-workspace-after-build-rollback");
    const build = async (slot: string) => createHash("sha256")
      .update(await readFile(resolve(root, "apps/web", `.next-private${slot}`, "BUILD_ID"))).digest("hex");
    const previousBuild = await build("");
    const candidateBuild = await build("-candidate");
    expect(previousBuild).not.toBe(candidateBuild);
    await writeFile(join(evidence, "recovery_observations.json"), JSON.stringify({
      active_and_queued: "FAILED/SERVICE_RESTART", incomplete_result_absent: true, pending_draft_preserved: true,
      backup_integrity: true, isolated_restore_integrity: true, restored_sessions_invalidated: true,
      reopened_after_restart_restore_and_rollback: true, verified_result_sha256: hash,
      previous_build_sha256: previousBuild, candidate_build_sha256: candidateBuild,
      release_scope: "Two independent production builds from one S8-C source revision and schema",
    }, null, 2));
    await rollback.context().close();
  } finally {
    await stop();
  }
});
