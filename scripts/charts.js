// Renders every patient's printed chart to PNG, one image per page.
//   npm run charts                 all patients  -> charts/<MRN>_<Name>/page-01.png ...
//   npm run charts -- <MRN> ...    only these patients
// Starts the app on a spare port, screenshots /ehr/patients/{id}/chart/{n}, then stops it.
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const python = process.platform === "win32"
  ? path.join(root, ".venv", "Scripts", "python.exe")
  : path.join(root, ".venv", "bin", "python");
const port = process.env.CHARTS_PORT || "8791";
const base = `http://127.0.0.1:${port}`;
const outDir = path.join(root, "charts");
const W = 1200, H = 1550;

async function waitForServer(server) {
  for (let i = 0; i < 120; i++) {
    if (server.exitCode !== null) throw new Error("The app exited before it was ready.");
    try { const r = await fetch(`${base}/api/health`); if (r.ok) return; } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("The app did not start within 60 seconds.");
}

(async () => {
  if (!fs.existsSync(python)) { console.error("Run `npm install` first."); process.exit(1); }
  const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--port", port], { cwd: root, stdio: "ignore" });
  try {
    await waitForServer(server);
    const wanted = new Set(process.argv.slice(2));
    const patients = (await (await fetch(`${base}/ehr/api/chart-pages`)).json())
      .filter((p) => !wanted.size || wanted.has(p.mrn));
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
    const index = [];
    let n = 0, overflow = [];
    for (const p of patients) {
      const dir = `${p.mrn}_${p.name.replace(/[^A-Za-z]+/g, "_")}`;
      fs.rmSync(path.join(outDir, dir), { recursive: true, force: true });
      fs.mkdirSync(path.join(outDir, dir), { recursive: true });
      const files = [];
      for (let i = 1; i <= p.pages; i++) {
        await page.goto(`${base}/ehr/patients/${p.id}/chart/${i}`);
        const h = await page.evaluate(() => document.querySelector(".bd").getBoundingClientRect().bottom);
        if (h > H - 44) overflow.push(`${p.mrn} page ${i}`);
        const file = `page-${String(i).padStart(2, "0")}.png`;
        await page.screenshot({ path: path.join(outDir, dir, file) });
        files.push(`${dir}/${file}`);
        n++;
      }
      index.push({ mrn: p.mrn, id: p.id, name: p.name, pages: files });
      process.stdout.write(`\r${index.length}/${patients.length} patients, ${n} pages`);
    }
    await browser.close();
    if (!wanted.size) fs.writeFileSync(path.join(outDir, "index.json"), JSON.stringify(index, null, 1));
    console.log(`\nWrote ${n} pages to ${outDir}`);
    if (overflow.length) console.warn(`Content may run into the footer on: ${overflow.join(", ")}`);
  } finally {
    server.kill();
  }
})().catch((e) => { console.error(e); process.exit(1); });
