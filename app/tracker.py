"""Serves the Referral Tracker page and builds its patient data from the same chart as the EHR."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel
from fastapi.responses import FileResponse

from . import llm
from .data import CONDITIONS, Chart
from .ehr_store import USERS, Store

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


def ehr_referrals(chart: Chart, store: Store) -> list[dict]:
    """The EHR's approved referral orders to this specialty for the tracker's patients.

    Only orders a person has approved are listed: a pending order is not real yet, so neither
    the offices nor the patient should see it. For each, the approved referral letter (if any)
    and, once the referral is closed, the consult note that closed it.
    """
    cfg = CONDITIONS[CONDITION]
    name = lambda uid: USERS.get(uid, {}).get("name", uid or "")
    out = []
    for row in chart.cohort(CONDITION):
        pid = row["id"]
        refs = [x for x in store.referrals(pid) if x["specialty"] == cfg["specialty"] and x["status"] in ("ordered", "closed")]
        if not refs:
            continue
        ref = refs[0]  # the most recent
        docs = [d for d in store.documents(pid) if d["referral_id"] == ref["id"] and d["status"] == "filed"]
        letter = next((d for d in docs if d["kind"] == "referral_letter"), None)
        consult = next((d for d in docs if d["kind"] == "consult_note"), None)
        out.append({
            "pid": pid[:8], "ehr": pid, "id": ref["id"], "status": ref["status"],
            "urgency": {"Emergent": "Emergency"}.get(ref["urgency"], ref["urgency"]),
            "question": ref["question"],
            "entered_by": name(ref["entered_by"]), "entered_by_ai": USERS.get(ref["entered_by"], {}).get("role") == "agent",
            "approved_by": name(ref["decided_by"]),
            "letter": {"id": letter["id"], "body": letter["body"], "by": name(letter["entered_by"]),
                       "by_ai": USERS.get(letter["entered_by"], {}).get("role") == "agent",
                       "approved_by": name(letter["decided_by"])} if letter else None,
            "consult": {"id": consult["id"], "title": consult["title"]} if consult and ref["status"] == "closed" else None,
        })
    return out


def build_router(chart: Chart, store: Store) -> APIRouter:
    r = APIRouter()

    @r.get("/api/tracker/ehr-referrals")
    def referrals_from_ehr() -> list[dict]:
        return ehr_referrals(chart, store)

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
