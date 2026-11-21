"""Runs of the Fable operator started from the EHR's Run Fable button.

Each run is `python -m app.fable_operator ...` in its own process. Its output goes to
var/fable-runs/<id>.log and its latest screen to <id>.png; the run page shows both live, so Fable
can be watched from any browser. FABLE_HEADED=0 (for a server with no screen) skips the separate
browser window that otherwise also opens on this machine.
One run at a time: two operators driving the EHR at once would only get in each other's way.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Runs:
    def __init__(self) -> None:
        self.dir = ROOT / "var" / "fable-runs"
        self.runs: dict[int, dict] = {}
        self.lock = threading.Lock()

    def active(self) -> dict | None:
        return next((r for r in self.runs.values() if r["proc"].poll() is None), None)

    def start(self, pid: str, mrn: str, specialty: str, urgency: str, question: str, base_url: str) -> int:
        """Starts a run and returns its id; if one is already running, returns that one's id."""
        with self.lock:
            if (running := self.active()):
                return running["id"]
            self.dir.mkdir(parents=True, exist_ok=True)
            rid = max(self.runs, default=0) + 1
            log = self.dir / f"{int(time.time())}-{rid}.log"
            cmd = [sys.executable, "-u", "-m", "app.fable_operator", "--mrn", mrn, "--specialty", specialty,
                   "--urgency", urgency, "--question", question]
            if os.environ.get("FABLE_HEADED", "1") != "0":
                cmd.append("--headed")
            frame = log.with_suffix(".png")
            with open(log, "wb") as out:
                proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                                        env={**os.environ, "EHR_URL": base_url, "PYTHONUNBUFFERED": "1",
                                             "FABLE_LIVE_FRAME": str(frame)})
            self.runs[rid] = {"id": rid, "pid": pid, "mrn": mrn, "specialty": specialty, "proc": proc,
                              "log": log, "frame": frame, "started": time.strftime("%H:%M:%S")}
            return rid

    def get(self, rid: int) -> dict | None:
        run = self.runs.get(rid)
        if not run:
            return None
        code = run["proc"].poll()
        status = "running" if code is None else "finished" if code == 0 else "failed"
        text = run["log"].read_text(encoding="utf-8", errors="replace") if run["log"].exists() else ""
        return {**run, "status": status, "text": text}
