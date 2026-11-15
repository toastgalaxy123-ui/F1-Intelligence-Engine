// Capture the real Streamlit release UI through Chrome's local DevTools socket.
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const [output, executable, url = "http://127.0.0.1:8519"] = process.argv.slice(2);
if (!output || !executable) {
  throw new Error("Usage: node scripts/capture_s6c.mjs OUTPUT CHROME_EXECUTABLE [LOCAL_DASHBOARD_URL]");
}
const profile = await mkdtemp(join(tmpdir(), "s6c-chrome-"));
await mkdir(join(output, "screenshots"), { recursive: true });
const browser = spawn(executable, [
  "--headless", "--disable-gpu", "--disable-extensions", "--no-first-run", "--remote-debugging-port=0",
  "--window-size=1512,1100", `--user-data-dir=${profile}`, "about:blank",
], { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });

const deadline = (promise, description) => {
  let timeout;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error(`Timed out: ${description}`)), 45000);
    }),
  ]).finally(() => clearTimeout(timeout));
};

try {
  const port = await deadline(new Promise((resolve, reject) => {
    browser.once("error", reject);
    browser.once("exit", (code) => reject(new Error(`Chrome exited early (${code})`)));
    browser.stderr.on("data", (buffer) => {
      const match = buffer.toString().match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/);
      if (match) resolve(match[1]);
    });
  }), "opening Chrome");
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const socket = new WebSocket(pages.find((item) => item.type === "page").webSocketDebuggerUrl);
  await deadline(new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  }), "connecting to Chrome");
  const pending = new Map();
  let id = 0;
  socket.onmessage = ({data}) => {
    const msg = JSON.parse(data);
    if (!msg.id) return;
    const [resolve, reject] = pending.get(msg.id) ?? [];
    pending.delete(msg.id);
    if (msg.error) reject?.(new Error(JSON.stringify(msg.error)));
    else resolve?.(msg.result);
  };
  function command(method, params = {}) {
    return deadline(new Promise((resolve, reject) => {
      const current = ++id;
      pending.set(current, [resolve, reject]);
      socket.send(JSON.stringify({id: current, method, params}));
    }), method);
  }
  async function evaluate(expression) {
    const {result, exceptionDetails} = await command("Runtime.evaluate", {expression, returnByValue: true});
    if (exceptionDetails) throw new Error(exceptionDetails.exception.description);
    return result.value;
  }
  async function waitFor(expression) {
    for (let i = 0; i < 120; i++) {
      if (await evaluate(expression)) return;
      const errors = await evaluate("[...document.querySelectorAll('[data-testid=stException]')].map(x=>x.innerText)");
      if (errors.length) throw new Error(errors.join("\n"));
      await delay(300);
    }
    const body = await evaluate("document.body.innerText.slice(0, 2500)");
    throw new Error(`Page did not show ${expression}\n${body}`);
  }
  const content = (t) => `document.body?.innerText.includes(${JSON.stringify(t)})`;
  async function click(selector) {
    const box = await evaluate(`(() => { const e = ${selector}; if (!e) return null;
      e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
      return { x: r.left + r.width/2, y: r.top + r.height/2 }; })()`);
    if (!box) throw new Error(`Control unavailable: ${selector}`);
    await command("Input.dispatchMouseEvent", {type: "mousePressed", button: "left", clickCount: 1, ...box});
    await command("Input.dispatchMouseEvent", {type: "mouseReleased", button: "left", clickCount: 1, ...box});
  }
  const control = (selector, text) =>
    `[...document.querySelectorAll(${JSON.stringify(selector)})].find(e=>e.textContent.trim()===${JSON.stringify(text)})`;
  async function button(text) {
    await waitFor(`!!(${control("button", text)})`);
    await click(control("button", text));
  }
  async function select(label, text) {
    await click(`[...document.querySelectorAll('[data-testid=stSelectbox]')].find(e=>e.innerText.includes(${
      JSON.stringify(label)})).querySelector('[role=combobox]')`);
    await waitFor(`!!(${control("[role=option]", text)})`);
    await click(control("[role=option]", text));
  }
  async function page(name, marker) {
    await button(name);
    await waitFor(content(marker));
  }
  async function run(marker) {
    await button("Run research analysis");
    await waitFor(content(marker));
  }
  const captured = [];
  async function capture(name, anchor = null) {
    await evaluate(`(() => {
      const main = document.querySelector('[data-testid=stMain]');
      const marker = ${JSON.stringify(anchor)};
      const e = marker && [...main.querySelectorAll('h2,h3,h4,p,label,[data-testid=stPlotlyChart]')]
        .find(node => node.textContent.includes(marker));
      if (e) e.scrollIntoView({block: 'start'});
      else { main.scrollTop = 0; document.documentElement.scrollTop = 0; }
    })()`);
    await delay(500);
    const {data} = await command("Page.captureScreenshot", {format: "png", fromSurface: true});
    const file = `${name}.png`;
    await writeFile(join(output, "screenshots", file), Buffer.from(data, "base64"));
    captured.push(file);
    console.log(file);
  }
  await command("Emulation.setDeviceMetricsOverride", {
    width: 1512, height: 1150, deviceScaleFactor: 1, mobile: false,
  });
  await command("Page.navigate", {url});
  await waitFor(content("Decision overview"));
  await run("No unique recommendation.");
  await capture("01-decision-overview");
  await page("Evidence & Validation", "Archive and replay provenance");
  await capture("02-historical-snapshot-provenance");
  await capture("22-model-support", "Model support");
  await click(control("summary p", "Frozen source hashes"));
  await capture("23-source-replay-provenance", "Frozen source hashes");
  await page("Race State", "Race state at cutoff");
  await capture("03-supported-and-unavailable-fields", "Observed and declared fields");
  await page("Forecasts", "D8R1 stint pace");
  await waitFor(content("Displayed forecast plans"));
  await capture("05-archived-mean-stint-forecast");

  await select("Snapshot preset", "Declared GREEN / VSC / SC");
  await waitFor(content("Inputs changed."));
  await page("Overview", "Decision overview");
  await run("Evaluated futures");
  await capture("19-no-unique-recommendation");
  await page("Race State", "Race state at cutoff");
  await capture("04-declared-race-state");
  await page("Forecasts", "D8R1 stint pace");
  await capture("06-fresh-versus-used-tyres");
  await page("Strategy Comparison", "Inspect candidate trace");
  await capture("07-manual-comparison");
  await button("Conditional optimization");
  await waitFor(content("Decision information"));
  await click(control("label", "DECLARED_FUTURE"));
  await waitFor("!!document.querySelector('[data-testid=stRadio] input:checked') && "
    + "Array.from(document.querySelectorAll('[data-testid=stRadio] label')).some("
    + "e=>e.textContent==='DECLARED_FUTURE' && e.querySelector('input')?.checked)");
  await capture("08-conditional-rankings");
  await select("Inspect candidate trace", "63-L31-soft");
  await waitFor(content("Trajectory, commitment and constraints"));
  await capture("09-clean-air-and-effective-time", "Clean-air and effective checkpoint time");
  await capture("10-following-constraints", "Strategy comparison");
  await capture("11-pit-execution", "Pit entry, service and rejoin");

  await page("Robust Strategy", "Regret matrix");
  await capture("13-robust-regret-matrix");
  await button("Pareto and oracle");
  await waitFor(content("Pareto frontier"));
  await capture("14-pareto-alternatives");
  await button("Declared futures");
  await waitFor(content("Evaluated"));
  await capture("12-green-vsc-sc-futures");
  await capture("15-sc-timing-sensitivity", "Evaluated");
  await page("Timing Risk", "46.754185 s");
  await capture("16-frozen-timing-failure");
  await select("Frozen policy trace", "p2-green-replan");
  await capture("17-p2-cancellation-and-replan", "Frozen policy trace");
  await select("Frozen policy trace", "p5-scheduled-conditional");
  await capture("18-p5-scheduled-conditional", "Frozen policy trace");

  await page("Overview", "Decision overview");
  const budget = "[...document.querySelectorAll('[data-testid=stNumberInput]')]"
    + ".find(e=>e.innerText.includes('Conditional search budget')).querySelector('input')";
  await click(budget);
  await evaluate(`(${budget}).select()`);
  await command("Input.insertText", {text: "2"});
  if (await evaluate(`(${budget}).value`) !== "2") throw new Error("Search budget edit did not reach input");
  await command("Input.dispatchKeyEvent", {type: "rawKeyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13});
  await command("Input.dispatchKeyEvent", {type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13});
  await waitFor(content("Inputs changed."));
  await run("Search incomplete.");
  await capture("20-budget-incomplete");
  await select("Snapshot preset", "Unsupported full-field reconstruction");
  await waitFor(content("FULL_FIELD_PHYSICAL_STATE_UNAVAILABLE"));
  await page("Race State", "Race state at cutoff");
  await capture("21-unsupported-historical-field");
  await page("Overview", "Decision overview");
  await capture("24-research-readiness");
  const report = {browser: "Chrome CDP", entrypoint: "Streamlit", captured,
                  validated: ["preset change invalidates analysis", "run analysis replaces invalidated output",
                    "candidate and frozen-policy selection", "search-budget change withholds recommendation",
                    "unsupported historical reconstruction disables execution", "evidence and replay provenance"]};
  await writeFile(join(output, "ui_validation.json"), JSON.stringify(report, null, 2) + "\n");
  socket.close();
} finally {
  browser.kill();
}
