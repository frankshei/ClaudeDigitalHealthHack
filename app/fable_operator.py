"""The Fable operator: Claude Fable 5.2 reads a patient's chart images, then works the EHR.

    python -m app.fable_operator --mrn 76920746 --specialty Endocrinology \
        --question "Next step in glucose-lowering treatment?"

Phase 1 (vision): the chart's printed pages (charts/<MRN>_*/page-NN.png, from `npm run charts`)
go to Fable 5.2, which drafts the referral: clinical question, reason, and the referral letter.

Phase 2 (computer use): Fable 5.2 drives the EHR in a browser through the computer use toolset,
looking at screenshots and clicking and typing. It places the referral order and files the
letter against it. Both stay pending until Dana or Dr. Patel approves them in the In basket.

The limits are enforced here as well as by the EHR server:
- The harness signs the browser in as the Fable operator. The demo sign-in has no password, so
  the harness blocks any request that would sign in or out, approve or reject, or reset data.
- The browser stays inside the EHR; anything else is sent back and reported as an error.
- The run stops after a fixed number of model turns.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import anthropic

from .ehr_store import SPECIALTIES, URGENCIES
from .llm import SYSTEM as LETTER_SYSTEM

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("OPERATOR_MODEL", "claude-fable-5-2")
EFFORT = os.environ.get("OPERATOR_EFFORT", "high")
BASE = os.environ.get("EHR_URL", "http://localhost:8000").rstrip("/")
VIEWPORT = {"width": 1280, "height": 800}  # within the image limits, so screenshots need no scaling
# Set by the EHR's Run Fable button: where to keep the latest screen for its live view.
LIVE_FRAME = Path(os.environ["FABLE_LIVE_FRAME"]) if os.environ.get("FABLE_LIVE_FRAME") else None
MAX_TURNS = int(os.environ.get("OPERATOR_MAX_TURNS", "40"))
# Server-side refusal fallback, and progress notes between tool calls returned as short summaries.
BETAS = ["server-side-fallback-2026-07-01", "thinking-display-updates-2026-08-18"]
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."
# Requests the operator may never make, whatever it decides: these would let it act as a person.
# It may not start another Fable run either (POST /ehr/patients/<id>/fable, blocked below).
BLOCKED_POSTS = ("/ehr/login", "/ehr/logout", "/ehr/inbox/", "/ehr/admin/")


def log(kind: str, text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {kind:<8} {text}", flush=True)


# ---- phase 1: read the chart images -------------------------------------------------------

REFERRAL_SCHEMA = {
    "type": "object",
    "properties": {
        "specialty": {"type": "string", "enum": SPECIALTIES},
        "urgency": {"type": "string", "enum": URGENCIES},
        "clinical_question": {"type": "string"},
        "reason": {"type": "string"},
        "letter_title": {"type": "string"},
        "letter": {"type": "string"},
    },
    "required": ["specialty", "urgency", "clinical_question", "reason", "letter_title", "letter"],
    "additionalProperties": False,
}


def chart_pages(mrn: str) -> list[Path]:
    dirs = sorted((ROOT / "charts").glob(f"{mrn}_*"))
    if not dirs:
        log("charts", f"No chart images for MRN {mrn}; rendering them (npm run charts -- {mrn})")
        subprocess.run(["node", str(ROOT / "scripts" / "charts.js"), mrn], cwd=ROOT, check=True)
        dirs = sorted((ROOT / "charts").glob(f"{mrn}_*"))
    if not dirs:
        sys.exit(f"No patient with MRN {mrn}.")
    return sorted(dirs[0].glob("page-*.png"))


def read_chart(client: anthropic.Anthropic, mrn: str, specialty: str, urgency: str, question: str) -> dict:
    pages = chart_pages(mrn)
    log("vision", f"Sending {len(pages)} chart page images for MRN {mrn} to {MODEL}")
    content: list[dict] = []
    for i, page in enumerate(pages, 1):
        content.append({"type": "text", "text": f"Chart page {i} of {len(pages)}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.standard_b64encode(page.read_bytes()).decode()}})
    content.append({"type": "text", "text": f"""\
These images are the printed chart of the patient with MRN {mrn}, the only source for this referral.

<referral_request>
Referring to: {specialty}
Urgency: {urgency.lower()}
PCP's clinical question: {question.strip() or '(none given: suggest one, and mark it "[suggested - confirm]")'}
</referral_request>

Return the referral as JSON. "letter" is the full referral letter in the PCP's voice. "reason" is a \
short summary of the history and workup for the order form. Read values from the images exactly as \
printed; if a value you need is unreadable or absent, say so under "TO CONFIRM BEFORE SENDING" at the \
end of the letter rather than guessing."""})
    with client.beta.messages.stream(
        model=MODEL, max_tokens=64000, betas=BETAS[:1], fallbacks="default",
        system=LETTER_SYSTEM,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": REFERRAL_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason in ("refusal", "max_tokens"):
        sys.exit(f"Chart reading stopped: {msg.stop_reason}.")
    draft = json.loads(next(b.text for b in msg.content if b.type == "text"))
    # The PCP decides the specialty and urgency; keep theirs whatever the model returned.
    draft["specialty"], draft["urgency"] = specialty, urgency
    if question.strip():
        draft["clinical_question"] = question.strip()
    log("vision", f"Drafted: {draft['clinical_question']}")
    return draft


# ---- phase 2: drive the EHR --------------------------------------------------------------

KEYS = {"return": "Enter", "enter": "Enter", "ctrl": "Control", "control": "Control", "alt": "Alt",
        "shift": "Shift", "super": "Meta", "cmd": "Meta", "meta": "Meta", "escape": "Escape", "esc": "Escape",
        "tab": "Tab", "backspace": "Backspace", "delete": "Delete", "space": "Space", "home": "Home",
        "end": "End", "page_up": "PageUp", "page_down": "PageDown", "pageup": "PageUp", "pagedown": "PageDown",
        "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight"}


def pw_key(combo: str) -> str:
    """xdotool-style names ("ctrl+Return") to Playwright's ("Control+Enter")."""
    return "+".join(KEYS.get(k.strip().lower(), k.strip()) for k in combo.split("+"))


class Browser:
    """Carries out computer toolset actions on a Playwright page, inside the EHR only."""

    def __init__(self, page) -> None:
        self.page = page
        self.cursor = (VIEWPORT["width"] // 2, VIEWPORT["height"] // 2)

    def screenshot(self) -> list[dict]:
        # A capture that starts while a page is being replaced can stall; wait for the load and retry.
        for attempt in range(3):
            try:
                png = self.page.screenshot(type="png", timeout=8000)
                break
            except Exception:
                if attempt == 2:
                    raise
                self._wait_for_load()
        self._frame(png)
        data = base64.standard_b64encode(png).decode()
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]

    def _frame(self, png: bytes | None = None) -> None:
        """Writes what the browser shows to LIVE_FRAME, for the live view on the EHR's run page."""
        if not LIVE_FRAME:
            return
        try:
            png = png or self.page.screenshot(type="png", timeout=4000)
            tmp = LIVE_FRAME.with_suffix(".tmp")
            tmp.write_bytes(png)
            tmp.replace(LIVE_FRAME)  # whole frames only: the page never reads a half-written file
        except Exception:
            pass  # the live view is a convenience; never let it stop the run

    def _wait_for_load(self) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass

    def _move(self, xy) -> None:
        if xy is not None:
            x, y = int(xy[0]), int(xy[1])
            if not (0 <= x < VIEWPORT["width"] and 0 <= y < VIEWPORT["height"]):
                raise ValueError(f"Coordinate {[x, y]} is outside the {VIEWPORT['width']}x{VIEWPORT['height']} screen.")
            self.page.mouse.move(x, y)
            self.cursor = (x, y)

    def _with_modifiers(self, mods: str | None, fn) -> None:
        held = [pw_key(m) for m in (mods or "").split("+") if m.strip()]
        for k in held:
            self.page.keyboard.down(k)
        try:
            fn()
        finally:
            for k in reversed(held):
                self.page.keyboard.up(k)

    def _settle(self) -> None:
        # A click or Enter can start a page load a moment later; give it time to begin, then finish.
        self.page.wait_for_timeout(300)
        self._wait_for_load()
        u = urlparse(self.page.url)
        if self.page.url != "about:blank" and (f"{u.scheme}://{u.netloc}" != BASE or not u.path.startswith("/ehr")):
            self.page.goto(f"{BASE}/ehr")
            raise RuntimeError("That left the EHR, which this operator may not do. Returned to Patient lookup.")

    def run(self, name: str, inp: dict) -> str | list[dict]:
        try:
            return self._run(name, inp)
        finally:
            if name != "screenshot":
                self._frame()

    def _run(self, name: str, inp: dict) -> str | list[dict]:
        m, kb = self.page.mouse, self.page.keyboard
        clicks = {"left_click": ("left", 1), "right_click": ("right", 1), "middle_click": ("middle", 1),
                  "double_click": ("left", 2), "triple_click": ("left", 3)}
        if name == "screenshot":
            return self.screenshot()
        if name in clicks:
            button, count = clicks[name]
            self._move(inp.get("coordinate"))
            self._with_modifiers(inp.get("text"), lambda: m.click(*self.cursor, button=button, click_count=count))
        elif name == "left_click_drag":
            self._move(inp["start_coordinate"])
            def drag():
                m.down()
                self._move(inp["coordinate"])
                m.up()
            self._with_modifiers(inp.get("text"), drag)
        elif name == "mouse_move":
            self._move(inp["coordinate"])
        elif name == "left_mouse_down":
            m.down()
        elif name == "left_mouse_up":
            m.up()
        elif name == "cursor_position":
            return f"[{self.cursor[0]}, {self.cursor[1]}]"
        elif name == "scroll":
            self._move(inp.get("coordinate"))
            step = 100 * int(inp.get("scroll_amount", 3))
            dx, dy = {"up": (0, -step), "down": (0, step), "left": (-step, 0), "right": (step, 0)}[inp["scroll_direction"]]
            self._with_modifiers(inp.get("text"), lambda: m.wheel(dx, dy))
        elif name == "type":
            kb.type(inp["text"])
        elif name == "key":
            for _ in range(int(inp.get("repeat", 1))):
                kb.press(pw_key(inp["text"]))
        elif name == "hold_key":
            k = pw_key(inp["text"])
            kb.down(k)
            time.sleep(min(float(inp.get("duration", 1)), 10))
            kb.up(k)
        elif name == "wait":
            time.sleep(min(float(inp.get("duration", 1)), 10))
        else:
            raise ValueError(f"{name} is not available in this environment.")
        self._settle()
        return "OK"


OPERATOR_SYSTEM = """\
You are the Fable operator, an AI agent working in Harborview EHR, a hospital's electronic \
health record, through the computer use tools. You are already signed in as "Fable operator". \
The screen is a browser showing the EHR, 1280x800 pixels.

What you may do: open the patient's chart, place one referral order on the Referrals tab, and \
file one referral letter on the Documents tab, linked to that order. Everything you enter is saved \
as pending, and a person (Dana or Dr. Patel) approves it. That review is the point: do not try to \
approve, reject, sign in or out, or reset anything; those actions are blocked and will fail.

Work only on the patient named in the task; check the name and MRN in the banner before you enter \
anything. Enter the text you are given exactly. If the EHR shows something unexpected (a different \
patient, an error, an existing pending order for the same specialty), stop and say what you saw \
instead of working around it. When both items are saved, say in one or two sentences what you \
entered and their status, and stop."""


def operate(client: anthropic.Anthropic, mrn: str, draft: dict, headed: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    task = f"""\
Patient MRN: {mrn}

1. Find the patient (search box in the top bar, or Patient lookup) and open their chart.
2. On the Referrals tab, submit a new referral order with:
   Refer to: {draft['specialty']}
   Urgency: {draft['urgency']}
   Clinical question: {draft['clinical_question']}
   Reason for referral: {draft['reason']}
3. On the Documents tab, file a document:
   Document type: Referral letter
   Referral: the order you just placed
   Title: {draft['letter_title']}
   Source: Drafted by the Fable operator from the printed chart ({mrn})
   Text: the letter below, exactly.

<letter>
{draft['letter']}
</letter>"""
    with sync_playwright() as p:
        exe = os.environ.get("OPERATOR_CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
        browser = p.chromium.launch(headless=not headed, executable_path=exe)
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        host = urlparse(BASE).hostname
        ctx.add_cookies([{"name": "ehr_user", "value": "fable", "domain": host, "path": "/"}])

        def guard(route, request):
            path = urlparse(request.url).path
            if request.method == "POST" and (path.startswith(BLOCKED_POSTS) or path.endswith("/fable")):
                log("blocked", f"POST {path}: the operator may not do this")
                return route.fulfill(status=403, content_type="text/html",
                                     body="<h1>Blocked</h1><p>The Fable operator may not sign in or out, approve, reject or reset.</p>")
            return route.continue_()

        ctx.route("**/*", guard)
        page = ctx.new_page()
        page.goto(f"{BASE}/ehr")
        screen = Browser(page)
        tools = [{"type": "computer_toolset_20260801", "configs": {"zoom": {"enabled": False}},
                  "cache_control": {"type": "ephemeral"}}]
        messages: list = [{"role": "user", "content": [{"type": "text", "text": task}, *screen.screenshot()]}]

        for turn in range(1, MAX_TURNS + 1):
            with client.beta.messages.stream(
                model=MODEL, max_tokens=64000, betas=BETAS, fallbacks="default",
                system=OPERATOR_SYSTEM, tools=tools,
                thinking={"type": "adaptive", "display": "updates"},
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},
                messages=messages,
            ) as stream:
                msg = stream.get_final_message()
            messages.append({"role": "assistant", "content": msg.content})  # unchanged, thinking blocks included
            for b in msg.content:
                if b.type == "thinking" and b.thinking:
                    log("fable", b.thinking)
                elif b.type == "text" and b.text.strip():
                    log("fable", b.text.strip())
            if msg.stop_reason == "refusal":
                log("stopped", "The model declined to continue.")
                break
            if msg.stop_reason == "max_tokens":
                log("stopped", "Output limit reached.")
                break

            results, failed = [], False
            for b in msg.content:
                if b.type != "tool_use":
                    continue
                result = {"type": "tool_result", "tool_use_id": b.id, "toolset_name": "computer"}
                if getattr(b, "toolset_name", None) != "computer":
                    result.update(content=f"Unknown tool {b.name}.", is_error=True)
                elif failed:
                    result.update(content=NOT_EXECUTED, is_error=True)
                else:
                    log("action", f"{b.name} {json.dumps(b.input)[:160] if b.input else ''}")
                    try:
                        out = screen.run(b.name, b.input or {})
                        result["content"] = out if isinstance(out, list) else [{"type": "text", "text": out}]
                    except Exception as e:
                        log("error", str(e))
                        result.update(content=f"Error: {e}", is_error=True)
                        failed = True
                results.append(result)
            if not results:
                log("done", f"Finished after {turn} turn(s). Pending items are in {BASE}/ehr/inbox for approval.")
                break
            messages.append({"role": "user", "content": results})
        else:
            log("stopped", f"Reached the limit of {MAX_TURNS} turns.")
        browser.close()


def load_env() -> None:
    """Reads KEY=VALUE lines from .env, as the server does, so the API key need not be exported."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mrn", required=True, help="Patient MRN as shown in the EHR")
    ap.add_argument("--specialty", default="Endocrinology", choices=SPECIALTIES)
    ap.add_argument("--urgency", default="Routine", choices=URGENCIES)
    ap.add_argument("--question", default="", help="The PCP's clinical question (Fable suggests one if omitted)")
    ap.add_argument("--headed", action="store_true", help="Show the browser window")
    ap.add_argument("--read-only", action="store_true", help="Only read the chart and print the draft")
    a = ap.parse_args()
    client = anthropic.Anthropic()
    try:
        draft = read_chart(client, a.mrn, a.specialty, a.urgency, a.question)
        print(json.dumps(draft, indent=2))
        if not a.read_only:
            operate(client, a.mrn, draft, a.headed)
    except anthropic.AuthenticationError:
        sys.exit("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.PermissionDeniedError as e:
        sys.exit(f"This API key cannot use {MODEL}: {e.message}")
    except anthropic.RateLimitError:
        sys.exit("Rate limited by the API. Wait a minute and run it again.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Could not reach the Claude API. Check the network connection.")


if __name__ == "__main__":
    main()
