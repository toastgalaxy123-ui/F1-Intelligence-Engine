/**
 * Capture scientific parity from an approved private origin using an interactive OIDC sign-in.
 * The short-lived cookie goes directly from the browser context to the Python verifier.
 */
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const [origin, output, ca] = process.argv.slice(2);
const url = origin && URL.canParse(origin) ? new URL(origin) : null;
if (!url || url.protocol !== "https:" || url.origin !== origin || !output ||
    /^(localhost\.?|127(?:\.\d+){3}|0\.0\.0\.0|\[::1?\])$/.test(url.hostname)) {
  throw new Error("Usage: node apps/web/scripts/check-private-parity.mjs HTTPS_ORIGIN OUTPUT_JSON [APPROVED_CA]");
}
const root = fileURLToPath(new URL("../../../", import.meta.url));
const browser = await chromium.launch({ channel: "chrome", headless: false });
let context;
let session;
try {
  context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(origin);
  process.stdout.write("Sign in with the approved invitee in Chrome. Leave the browser open while the analyses run.\n");
  const deadline = Date.now() + 300_000;
  while (Date.now() < deadline) {
    session = (await context.cookies(origin)).find(cookie =>
      cookie.name === "__Host-f1ri-session" && cookie.secure && cookie.httpOnly &&
      cookie.sameSite === "Lax" && cookie.domain === url.hostname && cookie.path === "/");
    if (session) break;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  if (!session) throw new Error("The approved session was not available before the sign-in window closed.");
  const args = ["scripts/verify_private_parity.py", "--external", "--origin", origin,
    "--output", resolve(output), ...(ca ? ["--ca", resolve(ca)] : [])];
  await new Promise((resolve, reject) => {
    const child = spawn("python", args, { cwd: root, stdio: ["pipe", "inherit", "inherit"] });
    child.once("error", reject);
    child.stdin.once("error", reject);
    child.once("close", code => code === 0 ? resolve() : reject(new Error("Private parity checks did not pass.")));
    child.stdin.end(session.value);
  });
} finally {
  if (session) {
    try {
      const response = await context.request.post(`${origin}/auth/logout`, {
        data: {}, headers: { Origin: origin }, timeout: 5_000, maxRedirects: 0,
      });
      if (!response.ok() && response.status() !== 401) throw new Error("Session logout failed.");
    } catch {
      process.stderr.write("Could not confirm app sign-out. Revoke the test session with the approved operator.\n");
      process.exitCode = 1;
    }
  }
  await browser.close();
}
