# Referrals with Claude Fable 5.2 (hackathon prototype)

A referral system built on a mock EHR, with synthetic patients only:

- **Harborview EHR** (mock): a traditional EHR over 120 synthetic Synthea patients.
- **Fable operator**: Claude Fable 5.2 reads a patient's chart *as images*, drafts the referral,
  then works the EHR *by computer use* (screenshots, clicks, typing) to place the order and file
  the letter. Everything it enters waits for a person to approve.
- **Referral Tracker**: PCP office, specialist office and patient views of a referral.
- **Referral note drafter**: the original drafting page.

## Quick start

You need Node.js, Python 3.10 or newer, and a Claude API key.

```
git checkout main && git pull
npm install                           # creates .venv and installs the Python packages
.venv/bin/playwright install chromium # once: the browser the operator drives (Windows: .venv\Scripts\playwright)
cp .env.example .env                  # then put your key after ANTHROPIC_API_KEY= (.env is not committed)
npm run dev                           # starts the app at http://localhost:8000
```

## Demo run-through

1. **Start the app** (terminal 1): `npm run dev`
2. **Run the Fable operator** (terminal 2), with the browser visible:

   ```
   npm run operator -- --mrn 76920746 --specialty Endocrinology --question "Next step in glucose-lowering treatment?" --headed
   ```

   - First Fable reads the patient's chart page images and prints the drafted referral
     (about a minute, no browser yet).
   - Then a browser window opens and Fable searches the MRN, places the referral order and
     files the referral letter. Don't click in that window while it works.
   - The terminal narrates each step: `fable` (its progress notes), `action` (each click or
     keystroke), `blocked` / `error` (anything the guard stopped).
3. **Approve as a person**: open http://localhost:8000/ehr/login, choose **Sign in as Dana**,
   then open the **In basket** (http://localhost:8000/ehr/inbox) and approve both items.
4. **Show the history**: the **Audit log** (http://localhost:8000/ehr/audit) shows what Fable
   proposed and what Dana approved.
5. **Run it again**: on the audit log page, signed in as Dana, click **Reset demo data**.

To see only the chart reading and the drafted referral: `npm run operator -- --mrn 76920746 --read-only`.
Any patient works: find MRNs in the EHR's Patient lookup.

## Where things are

| Page | URL |
|---|---|
| EHR patient lookup | http://localhost:8000/ehr |
| EHR sign-in (Fable operator, Dana, Dr. Patel) | http://localhost:8000/ehr/login |
| In basket (waiting for approval) | http://localhost:8000/ehr/inbox |
| Audit log | http://localhost:8000/ehr/audit |
| Referral Tracker | http://localhost:8000/tracker |
| Referral note drafter | http://localhost:8000 |

## Status

- Works: the EHR, chart images, EHR write-back with approval, the tracker, and the operator,
  tested with scripted actions against the real EHR.
- Not yet tried with the live model: the operator's computer-use phase with Fable 5.2 itself.
  If a run stumbles, keep the terminal output.
- Not connected yet: a referral the operator places in the EHR does not appear in the Referral
  Tracker; the tracker still runs its own demo referrals.

## Referral note drafter

A PCP picks a synthetic patient and a referral condition; the app builds a chart extract
from Synthea data and asks Claude to draft the referral note for the specialist.

Conditions: diabetic kidney disease (Nephrology), type 2 diabetes (Endocrinology), ischemic heart disease / heart failure
(Cardiology), knee or hip osteoarthritis (Orthopedic surgery).

## Run

Needs Python 3.10+ and Node.js (Node is only used to run the scripts).

```
npm install     # creates .venv and installs the Python packages
npm run dev     # starts the server with reload
```

Open http://localhost:8000. To use another port: set the `PORT` environment variable.

The flow: **Draft note** writes the note from the chart, Claude then asks follow-up
questions about the draft, and **Update note with answers** revises it.

## Going live

Copy `.env.example` to `.env`, paste the key after `ANTHROPIC_API_KEY=`, and restart the
server. The badge in the header switches from "Mock mode" to "Live". Without a key the
draft is a fixed template, not model output.

## Mock EHR

`npm run dev`, then open http://localhost:8000/ehr. A traditional, server-rendered EHR over
all 120 Synthea patients, for the operator agent to work in:

- Patient lookup by name or MRN (`/ehr?q=...`); an exact MRN opens the chart.
- Chart tabs at `/ehr/patients/{id}/{tab}`: summary, problems, medications, allergies,
  vitals, results (filter by category/test, paged), encounters (paged), procedures,
  imaging, careplans, demographics. Each encounter has a detail page with everything
  recorded at it.
- Plain links and GET forms only, so every screen has its own URL.

### Writing to the EHR (operator limits)

Sign in at `/ehr/login` as one of three demo users: **Fable operator** (AI agent),
**Dana** (referral coordinator) or **Dr. Patel**. Signed out, the EHR is read-only.

- The operator can do two things: place a referral order (Referrals tab) and file a document
  (Documents tab: referral letter, consult note, referral tracker note). Both are saved as
  **pending** and appear in the **In basket** (`/ehr/inbox`).
- Only Dana or Dr. Patel can approve or reject. The operator cannot, and cannot reset data.
- Approving a consult note linked to an open referral closes that referral.
- Nothing can change problems, medications, allergies, results or demographics.
- Every entry and decision is in the **Audit log** (`/ehr/audit`), with AI entries marked.
- The limits are enforced in `app/ehr_store.py`, not just hidden in the screens.
- Entries live in `var/ehr.db` (not in git; the Synthea CSVs are never changed). "Reset demo
  data" on the audit log page (signed in as a person) clears them.

## Fable operator

`app/fable_operator.py` runs Claude Fable 5.2 (`claude-fable-5-2`) in two phases:

1. **Vision:** sends the patient's chart page images (`charts/<MRN>_*/page-NN.png`, rendered
   first if missing) and drafts the referral: clinical question, reason and letter.
2. **Computer use:** drives the EHR in a browser through the computer use toolset
   (`computer_toolset_20260801`, 1280x800 screenshots), places the referral order and files the
   letter against it. Both stay pending for Dana or Dr. Patel to approve in the In basket.

With `npm run dev` running, in a second terminal:

```
npm run operator -- --mrn 76920746 --specialty Endocrinology --question "Next step in glucose-lowering treatment?"
```

Options: `--urgency Routine|Urgent|Emergent`, `--headed` (show the browser), `--read-only`
(phase 1 only). Needs `ANTHROPIC_API_KEY` in `.env` or the environment, and a Playwright
browser: run `.venv/bin/playwright install chromium` once (on Windows `.venv\Scripts\playwright`).
Settings: `OPERATOR_MODEL`, `OPERATOR_EFFORT` (default `high`), `OPERATOR_MAX_TURNS`
(default 40), `EHR_URL` (default `http://localhost:8000`).

The operator's limits, on top of the EHR's own: the harness signs the browser in as the Fable
operator and blocks any request that signs in or out, approves, rejects or resets, and sends
the browser back if it leaves the EHR. The demo sign-in has no password, so this harness
guard is what stops the agent signing in as a person; a real deployment needs real sign-in.

## Referral Tracker

http://localhost:8000/tracker: the team's Referral Tracker (PCP office, Endo office and
patient views for a type 2 diabetes referral to endocrinology), served by this app.

- Its patient data (`/tracker/data.json`) is built live from the same Synthea chart as the
  EHR (the `endo` condition in `app/data.py`), and each referral links to the patient's
  chart in the EHR.
- "Draft with Claude" streams from `/api/referrals`, the same drafting endpoint as the
  Referral Note Drafter. Patient messages are sorted by `/api/tracker/route-message`:
  status questions get an answer from the referral facts, everything else goes to a nurse.
- With no API key, drafts are labeled templates and every patient message goes to a nurse.
- Demo state (days, referrals) is kept in the browser's localStorage; "Reset demo" clears it.

## Chart images

`npm run charts` renders each patient's printed chart (`/ehr/patients/{id}/chart/{n}`)
to `charts/<MRN>_<Name>/page-NN.png`, 1200x1550 per page, plus `charts/index.json`.
`npm run charts -- <MRN>` renders one patient. Pages stay under ~1568 px so a vision
model reads them without downscaling.

A printed chart holds: allergies, active problems, active medications, recent vitals,
latest result per lab test (with the previous value), latest assessments, resolved
problems, medication history, care plans, imaging studies, procedures (grouped) and every
encounter. Full lab history is in the EHR's Results tab, not in the images.

## Layout

- `app/data.py`: loads the Synthea CSVs, defines the four referral conditions, builds the chart extract.
  Per-condition labs, relevance rules and specialist guidance live in `CONDITIONS`.
- `app/ehr.py`: the mock EHR screens and the paged printed chart.
- `app/tracker.py`: serves the Referral Tracker and builds its data.
- `app/ehr_store.py`: referral orders, documents, approvals and the audit log, with the write limits.
- `app/fable_operator.py`: the Fable 5.2 operator (chart images, then computer use in the EHR).
- `app/llm.py`: system prompt and the Claude API call (streaming).
- `app/main.py`: HTTP API. `POST /api/referrals` streams newline-delimited JSON events.
- `scripts/charts.js`: renders the chart PNGs with Playwright.
- `static/tracker/index.html`: the Referral Tracker page.
- `static/index.html`: the PCP-facing page.
- `data/synthea/`: Synthea sample export (120 synthetic patients, no real patient data).
