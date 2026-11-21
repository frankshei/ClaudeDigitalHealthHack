# Referral note drafting (hackathon prototype)

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

- `app/data.py`: loads the Synthea CSVs, defines the three cohorts, builds the chart extract.
  Per-condition labs, relevance rules and specialist guidance live in `CONDITIONS`.
- `app/ehr.py`: the mock EHR screens and the paged printed chart.
- `app/tracker.py`: serves the Referral Tracker and builds its data.
- `app/llm.py`: system prompt and the Claude API call (streaming).
- `app/main.py`: HTTP API. `POST /api/referrals` streams newline-delimited JSON events.
- `scripts/charts.js`: renders the chart PNGs with Playwright.
- `static/tracker/index.html`: the Referral Tracker page.
- `static/index.html`: the PCP-facing page.
- `data/synthea/`: Synthea sample export (120 synthetic patients, no real patient data).
