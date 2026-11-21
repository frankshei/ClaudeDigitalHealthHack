# Referral note drafting (hackathon prototype)

A PCP picks a synthetic patient and a referral condition; the app builds a chart extract
from Synthea data and asks Claude to draft the referral note for the specialist.

Conditions: diabetic kidney disease (Nephrology), ischemic heart disease / heart failure
(Cardiology), knee or hip osteoarthritis (Orthopedic surgery).

## Run

```
.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open http://localhost:8000.

First time on a new machine: `python -m venv .venv`, then
`.venv\Scripts\python -m pip install -r requirements.txt`.

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
