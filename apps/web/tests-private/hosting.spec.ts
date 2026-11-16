import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { account, api, capture, nav, origin, originHeaders, preset, query, signIn, submit } from "./fixtures";

const TEAM = ["4-L30-hard__63-L31-hard", "4-L30-hard__63-L32-soft"];
const stage = process.env.F1RI_STAGE_DIR!;
const stageOps = resolve("../../scripts/private_stage_fixture_ops.py");
const btn = (name: string) => ({ name, exact: true as const });

test("H1 H2 H11 — HTTPS sign-in, invitation, callback validation and sign-out", async ({ page, browser }) => {
  const denied = await page.request.get(`${api}/presets`);
  expect(denied.status()).toBe(401);
  expect(await (await page.request.post(`${api}/analyses`, { headers: originHeaders, data: {} })).status()).toBe(401);
  await page.goto("/");
  await expect(page.getByRole("heading", btn("Private research access"))).toBeVisible();
  await capture(page, "01-sign-in-required");
  expect((await page.request.get("/auth/callback?state=invalid&code=invalid")).status()).toBe(400);
  const uninvited = await browser.newContext({ baseURL: origin, ignoreHTTPSErrors: true });
  const visitor = await uninvited.newPage();
  await signIn(visitor, 2);
  await capture(visitor, "02-uninvited-identity-denied");
  await uninvited.close();

  await signIn(page);
  await capture(page, "03-authenticated-overview");
  const issued = (await page.context().cookies()).find(c => c.name === "__Host-f1ri-session");
  expect(issued?.secure && issued.httpOnly && issued.sameSite === "Lax" && issued.expires > Date.now() / 1000).toBeTruthy();
  let sessionRevoked!: () => void;
  let sendLogoutResponse!: () => void;
  const revoked = new Promise<void>(resolve => { sessionRevoked = resolve; });
  const releaseLogout = new Promise<void>(resolve => { sendLogoutResponse = resolve; });
  await page.route("**/auth/logout", async route => {
    const response = await route.fetch();
    sessionRevoked();
    await releaseLogout;
    await route.fulfill({ response });
  });
  try {
    await page.getByRole("button", btn("Sign out")).click();
    await revoked;
    const expired = page.waitForResponse(r => r.url() === `${origin}/auth/session` && r.status() === 401);
    const forbidden = page.waitForResponse(r => r.url() === `${origin}${api}/workspaces` && r.status() === 401);
    await page.evaluate(() => window.dispatchEvent(new Event("pageshow")));
    await nav(page, "Workspaces");
    await Promise.all([expired, forbidden]);
    await expect(page.getByRole("button", btn("Sign out"))).toBeDisabled();
  } finally {
    sendLogoutResponse();
  }
  await expect(page.getByRole("heading", btn("Signed out"))).toBeVisible();
  expect((await page.request.get(`${api}/workspaces`)).status()).toBe(401);
  const replay = await page.request.get("/auth/session", { headers: { Cookie: `__Host-f1ri-session=${issued?.value}` } });
  expect(replay.status()).toBe(401);
  await capture(page, "04-signed-out");
});

test("H3 H4 H5 H6 H18 H19 — signed-in research and original support restrictions", async ({ page }) => {
  await signIn(page);
  await nav(page, "Pace Forecast");
  await expect(page.getByRole("heading", btn("Lap and stint means"))).toBeVisible();
  await capture(page, "05-private-archived-forecast");
  await preset(page, "two-car");
  await nav(page, "Strategy Lab");
  for (const set of ["hard", "medium", "soft"]) {
    await page.getByLabel("Legal stop").selectOption(`63-L31-${set}`);
    await page.getByRole("button", btn("Add alternative")).click();
  }
  await page.getByRole("button", btn("Run comparison")).click();
  await expect(page.getByText("Following constraints make different pace forecasts", { exact: false })).toBeVisible();
  await capture(page, "06-private-declared-comparison");

  await preset(page, "neutralization");
  await nav(page, "Robust Strategy");
  await page.getByLabel("Robust search budget").fill("240");
  await page.getByRole("button", btn("Run robust comparison")).click();
  await expect(page.getByRole("heading", btn("Regret heatmap"))).toBeVisible();
  await capture(page, "07-private-robust-comparison");

  await nav(page, "Timing Risk");
  await page.getByRole("button", btn("Select the timing preset")).click();
  await page.getByRole("button", btn("Reproduce timing case")).click();
  await expect(page.getByText("46.754185 s", { exact: true })).toBeVisible();
  await capture(page, "08-private-safety-car-regret");

  await preset(page, "unsupported");
  await nav(page, "Strategy Lab");
  await expect(page.getByText("Physical strategy execution is unavailable", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", btn("Run comparison"))).toHaveCount(0);
  await page.getByText("Assumptions and context", { exact: true }).click();
  await page.getByLabel("Regulation era").selectOption("2026");
  await page.getByRole("button", btn("Apply context")).click();
  await expect(page.getByText("MODEL_ERA_UNSUPPORTED", { exact: true })).toBeVisible();
  await capture(page, "09-private-model-and-physical-gates");
});

test("H7 H8 H9 H10 H17 — team comparison, migration, two devices and cross-user denial", async ({ page, browser }) => {
  await signIn(page);
  await preset(page, "shared-box");
  await nav(page, "Strategy Lab");
  for (const policy of TEAM) {
    await page.getByLabel("Legal stop").selectOption(policy);
    await page.getByRole("button", btn("Add alternative")).click();
  }
  await page.getByRole("button", btn("Run comparison")).click();
  await expect(page.getByRole("heading", btn("Two-policy drilldown"))).toBeVisible();
  await page.getByLabel("Second policy", { exact: true }).selectOption(TEAM[1]);
  await expect(page.getByText("2.903848", { exact: true }).first()).toBeVisible();
  await capture(page, "10-private-shared-box");

  await page.getByRole("button", btn("Save this investigation")).click();
  const title = `Hosted team staging comparison ${randomUUID().slice(0, 8)}`;
  await page.getByLabel("Workspace title").fill(title);
  await page.getByRole("button", btn("Save current workspace")).click();
  await expect(page.getByText("Workspace saved.", { exact: true })).toBeVisible();
  const list = await (await page.request.get(`${api}/workspaces`)).json();
  const doc = list.find((w: { title: string }) => w.title === title);
  expect(Boolean(doc?.receipt?.result_sha256)).toBe(true);
  const path = `${api}/workspaces/${doc.id}`;
  await capture(page, "11-durable-account-workspaces");
  const download = page.waitForEvent("download");
  await page.getByRole("link", btn(`Export ${title}`)).click();
  const exported = JSON.parse(await readFile((await (await download).path())!, "utf8"));
  expect(exported.receipt.result_sha256).toBe(doc.receipt.result_sha256);

  const prior = { ...exported, id: randomUUID(), title: "Earlier browser investigation" };
  const badOriginDoc = { ...prior, id: randomUUID(), title: "Incompatible prior inputs", model_sha256: "0".repeat(64) };
  await page.evaluate(documents => localStorage.setItem("f1ri.workspaces.v1", JSON.stringify(documents)), [prior, badOriginDoc]);
  await nav(page, "Overview"); await nav(page, "Workspaces");
  await page.getByText("Import previous local workspaces", { exact: true }).click();
  await page.getByRole("button", btn("Copy Earlier browser investigation")).click();
  await expect(page.getByText("Local workspace copied to your account.", { exact: false })).toBeVisible();
  await page.getByRole("button", btn("Copy Incompatible prior inputs")).click();
  await expect(page.getByText("The saved model differs", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("f1ri.workspaces.v1")!).length)).toBe(2);
  const bad = await page.request.post(`${api}/workspaces`, { headers: originHeaders,
    data: { ...prior, id: randomUUID(), source_hashes: { invalid: "6".repeat(64) } } });
  expect((await bad.json()).error.code).toBe("WORKSPACE_SOURCE_MISMATCH");

  const phone = await browser.newContext({ baseURL: origin, viewport: { width: 390, height: 844 }, ignoreHTTPSErrors: true });
  const mobile = await phone.newPage();
  await signIn(mobile);
  await mobile.getByRole("button", btn("Open navigation")).click();
  await nav(mobile, "Workspaces");
  await mobile.getByRole("button", btn(`Reopen ${title}`)).click();
  await mobile.getByRole("button", btn("Reproduce saved analysis")).click();
  await expect(mobile.getByText("Saved result checksum verified", { exact: false })).toBeVisible();
  expect(await mobile.evaluate(() => document.documentElement.scrollWidth <= 390)).toBe(true);
  await capture(mobile, "12-mobile-second-device-replay");
  await phone.close();

  const another = await browser.newContext({ baseURL: origin, ignoreHTTPSErrors: true });
  const other = await another.newPage();
  await signIn(other, 1);
  await nav(other, "Workspaces");
  await expect(other.getByRole("heading", btn(title))).toHaveCount(0);
  for (const [method, suffix] of [["GET",""],["GET","/export"],["DELETE",""],["PUT",""],["POST","/duplicate"]]) {
    const res = await other.request.fetch(path+suffix, { method, headers: originHeaders,
      ...(method === "PUT" || method === "POST" ? { data: doc } : {}) });
    expect(res.status()).toBe(404);
  }
  await capture(other, "13-isolated-second-account");
  await another.close();
});

test("H12 H13 H14 H16 — expiry, owned job cancellation, private cache and rate limit", async ({ page, browser }) => {
  await signIn(page);
  const context = await browser.newContext({ baseURL: origin, ignoreHTTPSErrors: true });
  const other = await context.newPage();
  await signIn(other, 1);
  const q = await query(page.request, "neutralization", "robust", { budget: 128, robust_budget: 234 });
  const a = await submit(page.request, q);
  const dup = await submit(page.request, q);
  const b = await submit(other.request, q);
  expect(a.id === dup.id && a.id !== b.id).toBeTruthy();
  expect((await other.request.delete(`${api}/analyses/${a.id}`, { headers: originHeaders })).status()).toBe(404);
  expect((await other.request.get(`${api}/analyses/${a.id}`)).status()).toBe(404);
  const ownRequest = await page.request.delete(`${api}/analyses/${a.id}`, { headers: originHeaders });
  expect(ownRequest.status()).toBe(200);
  await expect.poll(async () => (await (await page.request.get(`${api}/analyses/${a.id}`)).json()).state).toBe("CANCELED");
  await expect.poll(async () => (await (await other.request.get(`${api}/analyses/${b.id}`)).json()).state).toBe("COMPLETED");
  const again = await submit(other.request, q);
  expect(again.cache_hit).toBe(true);
  let limited = false;
  for (let i=0; i<25; i++) {
    const attempt = await other.request.post(`${api}/analyses`, { data: q, headers: originHeaders });
    if (attempt.status() === 429 && (await attempt.json()).error.code === "RATE_LIMIT") { limited = true; break; }
  }
  expect(limited).toBe(true);
  expect((await page.request.get(`${api}/health`)).status()).toBe(200);
  const cookie = (await page.context().cookies()).find(c => c.name === "__Host-f1ri-session")!;
  const digest = createHash("sha256").update(cookie.value).digest("hex");
  execFileSync("python", [stageOps, stage, "expire", "-"], { input: digest, stdio: ["pipe", "ignore", "ignore"] });
  expect((await page.request.post(`${api}/analyses`, { headers: originHeaders, data: q })).status()).toBe(401);
  await page.reload();
  await expect(page.getByRole("heading", btn("Private research access"))).toBeVisible();
  await capture(page, "14-expired-session");
  const bAccount = await account(1);
  execFileSync("python", [stageOps, stage, "revoke", bAccount.subject], { stdio: "ignore" });
  try { expect((await other.request.get("/auth/session")).status()).toBe(401); }
  finally { execFileSync("python", [stageOps, stage, "enroll", bAccount.subject, bAccount.label], { stdio: "ignore" }); }
  await context.close();
});
