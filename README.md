# Referral note drafting (hackathon prototype)

A PCP picks a synthetic patient and a referral condition; the app builds a chart extract
from Synthea data and asks Claude to draft the referral note for the specialist.

Conditions: diabetic kidney disease (Nephrology), ischemic heart disease / heart failure
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

## Layout

- `app/data.py`: loads the Synthea CSVs, defines the three cohorts, builds the chart extract.
  Per-condition labs, relevance rules and specialist guidance live in `CONDITIONS`.
- `app/llm.py`: system prompt and the Claude API call (streaming).
- `app/main.py`: HTTP API. `POST /api/referrals` streams newline-delimited JSON events.
- `static/index.html`: the PCP-facing page.
- `data/synthea/`: Synthea sample export (120 synthetic patients, no real patient data).
