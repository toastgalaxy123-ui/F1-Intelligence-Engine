import { expect, type Page, type APIRequestContext } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const stage = process.env.F1RI_STAGE_DIR!;
export const origin = process.env.F1RI_PRIVATE_ORIGIN ?? "https://localhost:8443";
export const api = "/api/v1";
export const originHeaders = { Origin: origin };
type Account = { label: string; username: string; password: string; subject: string };

export async function account(person: 0 | 1 | 2): Promise<Account> {
  return JSON.parse(await readFile(join(stage, "accounts.private.json"), "utf8"))[person];
}

export async function signIn(page: Page, person: 0 | 1 | 2 = 0) {
  const user = await account(person);
  await page.goto(origin);
  await page.getByRole("link", { name: "Sign in with your invited account" }).click();
  await page.locator("#username").fill(user.username);
  await page.locator("#password").fill(user.password);
  await page.locator("#kc-login").click();
  if (person === 2) {
    await expect(page.getByRole("heading", { name: "Access denied" })).toBeVisible();
  } else {
    await expect(page.getByRole("heading", { name: "Singapore 2025", exact: true })).toBeVisible();
    await expect(page.getByText(user.label, { exact: true })).toBeVisible();
  }
}

export async function capture(page: Page, file: string) {
  const dir = process.env.F1RI_PRIVATE_SCREENSHOTS;
  if (dir) {
    await mkdir(dir, { recursive: true });
    await page.screenshot({ path: join(dir, `${file}.png`), animations: "disabled" });
  }
}

export function nav(page: Page, name: string) {
  return page.getByRole("navigation", { name: "Research workspace" }).getByRole("button", { name, exact: true }).click();
}

export async function preset(page: Page, id: string) {
  await page.getByLabel("Research preset", { exact: true }).selectOption(id);
}

export async function query(request: APIRequestContext, id: string, operation: string, fields = {}) {
  const s = await request.get(`${api}/presets/${id}/snapshot`);
  expect(s.status()).toBe(200);
  const snapshot = await s.json();
  return { context: snapshot.context, snapshot_id: snapshot.snapshot_id, operation, ...fields };
}

export async function submit(request: APIRequestContext, data: unknown) {
  const reply = await request.post(`${api}/analyses`, { data, headers: originHeaders });
  expect(reply.status()).toBe(202);
  return reply.json();
}
