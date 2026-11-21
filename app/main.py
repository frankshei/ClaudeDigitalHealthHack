"""HTTP API for the referral-note prototype. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Reads KEY=VALUE lines from .env so the API key need not be exported by hand."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env()  # before importing llm, which reads its settings from the environment

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import llm  # noqa: E402
from .data import CONDITIONS, Chart  # noqa: E402
from .ehr import build_router  # noqa: E402

app = FastAPI(title="Referral note drafting")
chart = Chart()
app.include_router(build_router(chart))


class Answer(BaseModel):
    question: str
    answer: str = ""


class ReferralRequest(BaseModel):
    patient_id: str
    condition: str
    clinical_question: str = ""
    urgency: str = "routine"
    notes: str = ""
    answers: list[Answer] = []
    draft: str = ""  # the current note text, when asking questions about it or revising it


def _extract(patient_id: str, condition: str) -> dict:
    if condition not in CONDITIONS:
        raise HTTPException(404, f"Unknown condition '{condition}'")
    try:
        return chart.extract(patient_id, condition)
    except KeyError:
        raise HTTPException(404, "Unknown patient")


@app.get("/api/health")
def health() -> dict:
    return {"mode": "live" if llm.is_live() else "mock", "model": llm.MODEL, "effort": llm.EFFORT}


@app.get("/api/conditions")
def conditions() -> list[dict]:
    return [{"key": k, "label": c["label"], "specialty": c["specialty"], "patients": len(chart.cohort(k))}
            for k, c in CONDITIONS.items()]


@app.get("/api/patients")
def patients(condition: str) -> list[dict]:
    if condition not in CONDITIONS:
        raise HTTPException(404, f"Unknown condition '{condition}'")
    return chart.cohort(condition)


@app.get("/api/patients/{patient_id}/extract")
def extract(patient_id: str, condition: str) -> dict:
    data = _extract(patient_id, condition)
    return {"patient": data["patient"], "text": data["text"]}


@app.post("/api/questions")
def questions(req: ReferralRequest) -> dict:
    """Follow-up questions for the PCP about a note that has just been drafted."""
    data = _extract(req.patient_id, req.condition)
    try:
        return llm.questions(data, req.clinical_question, req.urgency, req.notes, req.draft)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/referrals")
def referral(req: ReferralRequest) -> StreamingResponse:
    """Streams newline-delimited JSON events: text chunks, then one done or error event."""
    data = _extract(req.patient_id, req.condition)
    answers = [a.model_dump() for a in req.answers]

    def events():
        for event in llm.draft(data, req.clinical_question, req.urgency, req.notes, answers, req.draft):
            yield json.dumps(event) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")
