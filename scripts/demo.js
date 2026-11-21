// One command for the whole demo:   npm run demo
//
// 1. Sets up what is missing: the Python packages, the operator's browser, and .env.
// 2. Starts the app (or uses the one already running) and checks it sees your API key.
// 3. Clears earlier demo entries in the EHR, so every run starts fresh (--keep to skip).
// 4. Runs the Fable operator with the browser visible.
// 5. Opens your browser on the In basket sign-in, for you to approve as Dana.
//
// Options (all optional):  --mrn 76920746  --specialty Endocrinology  --urgency Routine
//                          --question "..."  --keep  --hide-browser
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const win = process.platform === "win32";
const python = path.join(root, ".venv", win ? "Scripts" : "bin", win ? "python.exe" : "python");
const port = process.env.PORT || "8000";
const base = `http://localhost:${port}`;

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 && args[i + 1] ? args[i + 1] : dflt; };
const mrn = opt("--mrn", "76920746");
const specialty = opt("--specialty", "Endocrinology");
const urgency = opt("--urgency", "Routine");
const question = opt("--question", "Next step in glucose-lowering treatment?");

const say = (msg) => console.log(`\n== ${msg}`);
const fail = (msg) => { console.error(`\n!! ${msg}\n`); process.exit(1); };
const run = (cmd, a, quiet = false) => spawnSync(cmd, a, { cwd: root, stdio: quiet ? "ignore" : "inherit" }).status === 0;

async function health() {
  try { const r = await fetch(`${base}/api/health`); return r.ok ? await r.json() : null; } catch { return null; }
}

function hasKey() {
  if (process.env.ANTHROPIC_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN) return true;
  const env = path.join(root, ".env");
  return fs.existsSync(env) && /^[ \t]*ANTHROPIC_API_KEY[ \t]*=[ \t]*[^\s#]+/m.test(fs.readFileSync(env, "utf8"));
}

function openBrowser(url) {
  const [cmd, a] = win ? ["cmd", ["/c", "start", "", url]] : process.platform === "darwin" ? ["open", [url]] : ["xdg-open", [url]];
  spawn(cmd, a, { stdio: "ignore", detached: true }).unref();
}

(async () => {
  // 1. Setup.
  if (!fs.existsSync(python)) {
    say("First run: installing the Python packages (a minute or two)");
    if (!run("node", [path.join(__dirname, "py.js"), "setup"])) fail("Setup failed. Check that Python 3.10 or newer is installed.");
  } else {
    run(python, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", "requirements.txt"]);
  }
  if (!fs.existsSync(path.join(root, ".env"))) fs.copyFileSync(path.join(root, ".env.example"), path.join(root, ".env"));
  if (!hasKey()) {
    fail(`No API key yet. Open this file:\n\n   ${path.join(root, ".env")}\n\n` +
         "and put your key after ANTHROPIC_API_KEY= (no spaces, no quotes needed), save it, then run: npm run demo");
  }
  // Same browser lookup as the operator: OPERATOR_CHROMIUM, a preinstalled one, or Playwright's own.
  const check = "import os\nfrom pathlib import Path\nfrom playwright.sync_api import sync_playwright as s\n" +
    "exe = os.environ.get('OPERATOR_CHROMIUM') or ('/opt/pw-browsers/chromium' if Path('/opt/pw-browsers/chromium').exists() else None)\n" +
    "with s() as p: p.chromium.launch(executable_path=exe).close()";
  const browserOk = run(python, ["-c", check], true);
  if (!browserOk) {
    say("Installing the browser the operator drives (once)");
    if (!run(python, ["-m", "playwright", "install", "chromium"])) fail("Could not install the browser. Try: .venv/bin/playwright install chromium");
  }

  // 2. The app.
  let server = null;
  let h = await health();
  if (h) {
    say(`Using the app already running at ${base}`);
  } else {
    say(`Starting the app at ${base} (its log: var/demo-server.log)`);
    fs.mkdirSync(path.join(root, "var"), { recursive: true });
    const logFile = fs.openSync(path.join(root, "var", "demo-server.log"), "w");
    server = spawn(python, ["-m", "uvicorn", "app.main:app", "--port", port], { cwd: root, stdio: ["ignore", logFile, logFile] });
    for (let i = 0; i < 120 && !h; i++) {
      if (server.exitCode !== null) fail("The app stopped while starting. See var/demo-server.log.");
      await new Promise((r) => setTimeout(r, 500));
      h = await health();
    }
    if (!h) fail("The app did not start within a minute. See var/demo-server.log.");
  }
  const stop = () => { if (server) server.kill(); };
  process.on("SIGINT", () => { stop(); process.exit(0); });
  if (h.mode !== "live") {
    stop();
    fail("The app is running without your API key (mock mode). Check ANTHROPIC_API_KEY in .env" +
         (server ? "." : ", then restart the app you already had running."));
  }

  // 3. Fresh start.
  if (!args.includes("--keep")) {
    say("Clearing earlier demo entries in the EHR");
    await fetch(`${base}/ehr/admin/reset`, { method: "POST", redirect: "manual", headers: { cookie: "ehr_user=dana" } });
  }

  // 4. Fable.
  say(`Fable 5.2 reads the chart images for MRN ${mrn}, then works the EHR in a browser window.\n` +
      "   Watch the window, but don't click in it while Fable works.");
  const opArgs = ["-m", "app.fable_operator", "--mrn", mrn, "--specialty", specialty, "--urgency", urgency, "--question", question];
  if (!args.includes("--hide-browser")) opArgs.push("--headed");
  const op = spawnSync(python, opArgs, { cwd: root, stdio: "inherit", env: { ...process.env, EHR_URL: base } });
  if (op.status !== 0) { stop(); fail("The operator stopped with an error (above). Paste that output to whoever is helping you."); }

  // 5. The human step.
  say("Fable is done. Your turn, as Dana:");
  console.log(`   1. In the browser that just opened, click "Sign in as Dana".
   2. In the In basket, click Approve on both items.
   3. Audit log, to see who did what:  ${base}/ehr/audit
   4. Referral Tracker (updates a few seconds after you approve):  ${base}/tracker
      Click "Patient" at the top to see what the patient sees.`);
  openBrowser(`${base}/ehr/login?next=/ehr/inbox`);
  if (server) {
    console.log("\n   The app keeps running so you can click through. Press Ctrl+C here when you're finished.");
    await new Promise(() => {});
  }
})().catch((e) => fail(e.message));
