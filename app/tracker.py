"""Serves the Referral Tracker page and builds its patient data from the same chart as the EHR."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel
from fastapi.responses import FileResponse

from . import llm
from .data import CONDITIONS, Chart

ROOT = Path(__file__).resolve().parent.parent
CONDITION = "endo"


class PatientMessage(BaseModel):
    facts: str
    message: str


def tracker_data(chart: Chart) -> dict:
    cfg = CONDITIONS[CONDITION]
    patients = []
    for row in chart.cohort(CONDITION):
        x = chart.extract(row["id"], CONDITION)
        patients.append({
            # The tracker keys patients by a short id; "ehr" is the full id for links into the EHR.
            "id": row["id"][:8], "ehr": row["id"],
            "name": row["name"], "age": row["age"], "sex": row["sex"], "headline": row["headline"],
            "extract": x["text"],
            "problems": [name for _, name in x["relevant_problems"]],
            "meds": sorted(x["active_meds"]),
            "allergies": x["allergies"],
            "results": [[r["label"], r["latest"][1], r["latest"][0]] for r in x["results"]
                        if r["label"] in cfg["panel"] and r["label"].split(",")[0] != "Diabetic retinopathy severity"][:8],
        })
    return {"system": llm.SYSTEM, "as_of": chart.today.isoformat(),
            "condition": {"key": CONDITION, "label": cfg["label"], "specialty": cfg["specialty"],
                          "guidance": cfg["guidance"], "patients": patients,
                          "fallback_questions": [{"question": q, "why": w} for q, w in llm.FALLBACK_QUESTIONS[CONDITION]]}}


def build_router(chart: Chart) -> APIRouter:
    r = APIRouter()

    @r.get("/tracker")
    def page() -> FileResponse:
        return FileResponse(ROOT / "static" / "tracker" / "index.html")

    @r.get("/tracker/data.json")
    def data() -> dict:
        return tracker_data(chart)

    @r.post("/api/tracker/route-message")
    def route_message(m: PatientMessage) -> dict:
        return llm.route_patient_message(m.facts[:2000], m.message[:2000])

    return r
