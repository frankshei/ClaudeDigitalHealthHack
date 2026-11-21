"""A mock, traditional EHR over the Synthea export: every patient, every table.

Server-rendered HTML with plain links and GET forms, so an operator agent can drive it by
URL, by clicking, or by reading the screen. Each patient also has a paged "printed chart"
(/ehr/patients/{id}/chart/{n}) sized to be screenshotted as one PNG per page.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date
from html import escape

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from .data import Chart, _clean, _d, _fmt

PRODUCT = "Harborview EHR"
PAGE_SIZE = 100
TABS = [("summary", "Summary"), ("problems", "Problems"), ("medications", "Medications"),
        ("allergies", "Allergies"), ("vitals", "Vitals"), ("results", "Results"),
        ("encounters", "Encounters"), ("procedures", "Procedures"), ("imaging", "Imaging"),
        ("careplans", "Care plans"), ("demographics", "Demographics")]

# Printed chart page geometry (CSS pixels). Kept under ~1568 px on the long edge so a
# vision model reads it at full resolution instead of downscaling it.
CHART_W, CHART_H = 1200, 1550
CHART_BODY_PX = 1330          # height available for sections below the banner, above the footer
CHAR_PX = 6.9                 # average character width at 13px Inter, a little generous
LINE_PX, ROW_PAD_PX = 18, 7   # one wrapped line, and a row's vertical padding + border
HEAD_PX, GAP_PX = 30 + 26, 14  # section title + column header row; space after a section


def _x(v) -> str:
    return escape("" if v is None else str(v))


def mrn(pid: str) -> str:
    return f"{int(pid.replace('-', '')[:10], 16) % 10**8:08d}"


def _kind(description: str) -> str:
    m = re.search(r"\(([^()]*)\)$", description)
    return m.group(1).capitalize() if m else ""


class EHR:
    """Per-patient views of the chart, shared by the screens and the printed chart."""

    def __init__(self, chart: Chart) -> None:
        self.c = chart
        self.by_mrn = {mrn(pid): pid for pid in chart.patients}

    # ---- identity ---------------------------------------------------------------

    def ident(self, pid: str) -> dict:
        p = self.c.patients[pid]
        name = " ".join(re.sub(r"\d+", "", p[k]) for k in ("FIRST", "MIDDLE", "LAST") if p[k])
        born = date.fromisoformat(p["BIRTHDATE"])
        end = date.fromisoformat(p["DEATHDATE"][:10]) if p["DEATHDATE"] else self.c.today
        age = end.year - born.year - ((end.month, end.day) < (born.month, born.day))
        encs = self.c.encounters.get(pid, [])
        return {
            "id": pid, "mrn": mrn(pid), "name": name,
            "list_name": f"{re.sub(r'[0-9]+', '', p['LAST'])}, {re.sub(r'[0-9]+', '', p['FIRST'])}",
            "dob": p["BIRTHDATE"], "age": age, "sex": {"M": "Male", "F": "Female"}.get(p["GENDER"], p["GENDER"]),
            "deceased": _d(p["DEATHDATE"]) if p["DEATHDATE"] else "",
            "city": f"{p['CITY']}, {p['STATE']}",
            "last_visit": max((_d(e["START"]) for e in encs), default=""),
        }

    def allergy_names(self, pid: str) -> list[str]:
        return sorted({_clean(a["DESCRIPTION"]) for a in self.c.allergies.get(pid, []) if not a["STOP"]})

    # ---- grouped tables -----------------------------------------------------------

    def problems(self, pid: str) -> tuple[list[dict], list[dict]]:
        g: dict[str, dict] = {}
        for r in self.c.conditions.get(pid, []):
            cur = g.setdefault(r["DESCRIPTION"], {"name": _clean(r["DESCRIPTION"]), "kind": _kind(r["DESCRIPTION"]),
                                                  "code": r["CODE"], "onset": _d(r["START"]), "resolved": "",
                                                  "episodes": 0, "active": False})
            cur["episodes"] += 1
            cur["onset"] = min(cur["onset"], _d(r["START"]))
            if r["STOP"]:
                cur["resolved"] = max(cur["resolved"], _d(r["STOP"]))
            else:
                cur["active"] = True
        active = sorted((v for v in g.values() if v["active"]), key=lambda v: (v["kind"] != "Disorder", v["onset"]))
        resolved = sorted((v for v in g.values() if not v["active"]), key=lambda v: v["resolved"], reverse=True)
        return active, resolved

    def medications(self, pid: str) -> tuple[list[dict], list[dict]]:
        g: dict[str, dict] = {}
        for r in self.c.medications.get(pid, []):
            cur = g.setdefault(r["DESCRIPTION"], {"name": r["DESCRIPTION"], "code": r["CODE"], "start": _d(r["START"]),
                                                  "stop": "", "last": _d(r["START"]), "orders": 0, "dispenses": 0,
                                                  "reason": _clean(r["REASONDESCRIPTION"]), "active": False})
            cur["orders"] += 1
            cur["dispenses"] += int(float(r["DISPENSES"] or 0))
            cur["start"] = min(cur["start"], _d(r["START"]))
            cur["last"] = max(cur["last"], _d(r["START"]))
            if r["STOP"]:
                cur["stop"] = max(cur["stop"], _d(r["STOP"]))
            else:
                cur["active"] = True
            if not cur["reason"]:
                cur["reason"] = _clean(r["REASONDESCRIPTION"])
        active = sorted((v for v in g.values() if v["active"]), key=lambda v: v["name"])
        past = sorted((v for v in g.values() if not v["active"]), key=lambda v: v["stop"], reverse=True)
        return active, past

    def allergies(self, pid: str) -> list[dict]:
        out = []
        for a in sorted(self.c.allergies.get(pid, []), key=lambda a: a["START"]):
            reactions = "; ".join(f"{_clean(a[f'DESCRIPTION{i}'])}" + (f" ({a[f'SEVERITY{i}'].lower()})" if a[f"SEVERITY{i}"] else "")
                                  for i in (1, 2) if a[f"DESCRIPTION{i}"])
            out.append({"name": _clean(a["DESCRIPTION"]), "type": a["TYPE"], "category": a["CATEGORY"],
                        "reactions": reactions, "onset": _d(a["START"]),
                        "status": f"Inactive since {_d(a['STOP'])}" if a["STOP"] else "Active"})
        return out

    def observations(self, pid: str) -> list[dict]:
        return sorted(self.c.observations.get(pid, []), key=lambda o: (o["DATE"], o["DESCRIPTION"]), reverse=True)

    def latest_by_test(self, pid: str, categories: set[str]) -> list[dict]:
        """Latest result per test, with the one before it."""
        series: dict[str, list[dict]] = defaultdict(list)
        for o in self.observations(pid):
            if o["CATEGORY"] in categories:
                series[o["DESCRIPTION"]].append(o)
        out = []
        for name, rows in series.items():
            out.append({"name": name, "category": rows[0]["CATEGORY"], "value": _val(rows[0]),
                        "date": _d(rows[0]["DATE"]),
                        "prior": f"{_val(rows[1])} ({_d(rows[1]['DATE'])})" if len(rows) > 1 else "",
                        "count": len(rows)})
        return sorted(out, key=lambda r: (r["category"], r["name"]))

    def vitals_flowsheet(self, pid: str, columns: int = 8) -> tuple[list[str], list[tuple[str, list[str]]]]:
        rows = [o for o in self.observations(pid) if o["CATEGORY"] == "vital-signs"]
        dates = sorted({_d(o["DATE"]) for o in rows}, reverse=True)[:columns]
        cell: dict[tuple[str, str], str] = {}
        names: dict[str, None] = {}
        for o in sorted(rows, key=lambda o: o["DATE"]):
            label = o["DESCRIPTION"] + (f" ({o['UNITS']})" if o["UNITS"] else "")
            names.setdefault(label)
            cell[(label, _d(o["DATE"]))] = _fmt(o["VALUE"])
        table = [(n, [cell.get((n, d), "") for d in dates]) for n in sorted(names)]
        return dates, [(n, vals) for n, vals in table if any(vals)]

    def encounters(self, pid: str) -> list[dict]:
        return sorted(self.c.encounters.get(pid, []), key=lambda e: e["START"], reverse=True)

    def procedures(self, pid: str) -> list[dict]:
        return sorted(self.c.procedures.get(pid, []), key=lambda p: p["START"], reverse=True)

    def procedures_grouped(self, pid: str) -> list[dict]:
        g: dict[str, dict] = {}
        for p in self.c.procedures.get(pid, []):
            cur = g.setdefault(p["DESCRIPTION"], {"name": _clean(p["DESCRIPTION"]), "n": 0, "first": _d(p["START"]),
                                                  "last": _d(p["START"]), "reason": _clean(p["REASONDESCRIPTION"])})
            cur["n"] += 1
            cur["first"] = min(cur["first"], _d(p["START"]))
            cur["last"] = max(cur["last"], _d(p["START"]))
        return sorted(g.values(), key=lambda v: v["last"], reverse=True)

    def imaging(self, pid: str) -> list[dict]:
        """One row per study (the export has one row per image instance)."""
        g: dict[str, dict] = {}
        for i in self.c.imaging.get(pid, []):
            cur = g.setdefault(i["Id"], {"date": _d(i["DATE"]), "modality": i["MODALITY_DESCRIPTION"],
                                         "site": _clean(i["BODYSITE_DESCRIPTION"]), "series": set(),
                                         "instances": 0, "encounter": i["ENCOUNTER"], "sop": i["SOP_DESCRIPTION"]})
            cur["series"].add(i["SERIES_UID"])
            cur["instances"] += 1
        return sorted(g.values(), key=lambda s: s["date"], reverse=True)

    def careplans(self, pid: str) -> list[dict]:
        rows = sorted(self.c.careplans.get(pid, []), key=lambda c: c["START"], reverse=True)
        return [{"name": _clean(c["DESCRIPTION"]), "reason": _clean(c["REASONDESCRIPTION"]), "start": _d(c["START"]),
                 "stop": _d(c["STOP"]), "status": "Completed" if c["STOP"] else "Active"} for c in rows]

    def encounter(self, pid: str, eid: str) -> dict | None:
        enc = next((e for e in self.c.encounters.get(pid, []) if e["Id"] == eid), None)
        if not enc:
            return None
        def of(rows): return [r for r in rows.get(pid, []) if r["ENCOUNTER"] == eid]
        return {"enc": enc, "conditions": of(self.c.conditions), "medications": of(self.c.medications),
                "observations": sorted(of(self.c.observations), key=lambda o: o["DESCRIPTION"]),
                "procedures": of(self.c.procedures), "careplans": of(self.c.careplans),
                "allergies": of(self.c.allergies),
                "imaging": [s for s in self.imaging(pid) if s["encounter"] == eid]}


def _val(o: dict) -> str:
    v = _clean(_fmt(o["VALUE"]))
    return f"{v} {o['UNITS']}" if o["UNITS"] else v


def _money(v: str) -> str:
    try:
        return f"${float(v):,.2f}"
    except ValueError:
        return v


# ---- HTML ---------------------------------------------------------------------------

CSS = """
:root{--navy:#1f3a5f;--navy2:#2b4f7e;--line:#c9d1dc;--soft:#eef2f7;--text:#1b2430;--muted:#5b6676;--alert:#b3261e;--link:#1a5ea8}
*{box-sizing:border-box}
body{margin:0;font:13px/1.4 Inter,"Segoe UI",Arial,sans-serif;color:var(--text);background:#e9edf2}
a{color:var(--link);text-decoration:none} a:hover{text-decoration:underline}
.top{display:flex;align-items:center;gap:18px;background:var(--navy);color:#fff;padding:7px 14px}
.top b{font-size:15px;letter-spacing:.02em} .top a{color:#dfe8f3} .top .sp{flex:1}
.top form{display:flex;gap:6px} .top input{padding:4px 8px;border:0;border-radius:3px;width:260px;font:inherit}
.top .who{color:#b9c8da;font-size:12px}
.banner{display:flex;flex-wrap:wrap;gap:6px 26px;align-items:baseline;background:#fff;border-bottom:3px solid var(--navy2);padding:9px 14px}
.banner .nm{font-size:19px;font-weight:700} .banner .f{color:var(--muted)} .banner .f b{color:var(--text);font-weight:600}
.banner .alg{color:var(--alert);font-weight:600} .dec{background:var(--alert);color:#fff;padding:1px 8px;border-radius:3px;font-weight:700}
.wrap{display:flex;min-height:calc(100vh - 100px)}
nav.tabs{width:170px;flex:none;background:#f7f9fb;border-right:1px solid var(--line);padding:8px 0}
nav.tabs a{display:block;padding:7px 14px;color:var(--text);border-left:3px solid transparent}
nav.tabs a.on{background:#dde7f3;border-left-color:var(--navy2);font-weight:600}
main{flex:1;min-width:0;padding:12px 16px}
h1{font-size:17px;margin:2px 0 10px} h2{font-size:13px;margin:16px 0 6px;text-transform:uppercase;letter-spacing:.04em;color:var(--navy)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:12px}
.card{background:#fff;border:1px solid var(--line);border-radius:4px;padding:2px 12px 10px}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #e1e6ed;vertical-align:top}
th{background:var(--soft);font-weight:600;color:#2c3a4d;white-space:nowrap}
tr:hover td{background:#f6f9fc}
td.n{text-align:right;white-space:nowrap} td.d{white-space:nowrap}
.muted{color:var(--muted)} .pill{display:inline-block;padding:0 7px;border-radius:9px;font-size:11px;border:1px solid var(--line)}
.pill.act{border-color:#2e7d32;color:#2e7d32} .pill.res{color:var(--muted)}
.bar{display:flex;gap:8px;align-items:center;margin:0 0 10px;flex-wrap:wrap}
.bar select,.bar input{padding:4px 6px;font:inherit;border:1px solid var(--line);border-radius:3px}
.bar button{padding:4px 12px;font:inherit;background:var(--navy2);color:#fff;border:0;border-radius:3px;cursor:pointer}
.pager{display:flex;gap:12px;align-items:center;margin:10px 0}
dl.kv{display:grid;grid-template-columns:190px 1fr;gap:4px 12px;margin:6px 0} dl.kv dt{color:var(--muted)} dl.kv dd{margin:0}
"""


def _page(title: str, body: str, banner: str = "") -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{_x(title)} | {PRODUCT}</title>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'><style>{CSS}</style></head><body>"
            f"<div class='top'><b>{PRODUCT}</b><a href='/ehr'>Patient lookup</a>"
            f"<span class='sp'></span><form action='/ehr' method='get'><input name='q' aria-label='Search patients' "
            f"placeholder='Search name or MRN'><button type='submit' style='display:none'>Search</button></form>"
            f"<span class='who'>Synthetic data (Synthea) · Training environment</span></div>{banner}{body}</body></html>")


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] = frozenset(), dates: set[int] = frozenset(),
           empty: str = "None recorded") -> str:
    if not rows:
        return f"<p class='muted'>{empty}</p>"
    def cls(i): return " class='n'" if i in numeric else " class='d'" if i in dates else ""
    head = "".join(f"<th>{_x(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td{cls(i)}>{c}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _pager(base: str, page: int, total: int, extra: str = "") -> str:
    pages = max(1, math.ceil(total / PAGE_SIZE))
    if pages == 1:
        return f"<div class='pager muted'>{total} records</div>"
    prev = f"<a href='{base}?page={page - 1}{extra}'>&larr; Previous</a>" if page > 1 else "<span class='muted'>&larr; Previous</span>"
    nxt = f"<a href='{base}?page={page + 1}{extra}'>Next &rarr;</a>" if page < pages else "<span class='muted'>Next &rarr;</span>"
    lo = (page - 1) * PAGE_SIZE + 1
    return (f"<div class='pager'>{prev}<span>Page {page} of {pages} · records {lo}–{min(total, page * PAGE_SIZE)} "
            f"of {total}</span>{nxt}</div>")


def _status(active: bool) -> str:
    return "<span class='pill act'>Active</span>" if active else "<span class='pill res'>Resolved</span>"


def build_router(chart: Chart) -> APIRouter:
    ehr = EHR(chart)
    r = APIRouter()

    def need(pid: str) -> dict:
        if pid not in chart.patients:
            raise HTTPException(404, "Unknown patient")
        return ehr.ident(pid)

    def banner(h: dict) -> str:
        alg = ehr.allergy_names(h["id"])
        alg_html = (f"<span class='alg'>Allergies: {_x(', '.join(alg))}</span>" if alg
                    else "<span class='f'>Allergies: <b>No known allergies</b></span>")
        dec = f"<span class='dec'>DECEASED {_x(h['deceased'])}</span>" if h["deceased"] else ""
        return (f"<div class='banner' id='patient-banner'><span class='nm'>{_x(h['name'])}</span>{dec}"
                f"<span class='f'>MRN <b>{h['mrn']}</b></span><span class='f'>DOB <b>{h['dob']}</b> ({h['age']} y)</span>"
                f"<span class='f'>Sex <b>{h['sex']}</b></span><span class='f'>Location <b>{_x(h['city'])}</b></span>"
                f"{alg_html}</div>")

    def chart_page(h: dict, tab: str, title: str, content: str) -> HTMLResponse:
        nav = "".join(f"<a href='/ehr/patients/{h['id']}/{k}' class='{'on' if k == tab else ''}'>{v}</a>" for k, v in TABS)
        nav += f"<a href='/ehr/patients/{h['id']}/chart/1'>Printable chart</a>"
        body = f"<div class='wrap'><nav class='tabs' aria-label='Chart sections'>{nav}</nav><main><h1>{_x(title)}</h1>{content}</main></div>"
        return HTMLResponse(_page(f"{h['name']} – {title}", body, banner(h)))

    # ---- patient lookup -------------------------------------------------------------

    @r.get("/ehr", response_class=HTMLResponse)
    def lookup(q: str = "", status: str = "all") -> HTMLResponse:
        q = q.strip()
        if q and q.isdigit() and q.zfill(8) in ehr.by_mrn:
            return RedirectResponse(f"/ehr/patients/{ehr.by_mrn[q.zfill(8)]}/summary", status_code=303)
        people = [ehr.ident(pid) for pid in chart.patients]
        if q:
            people = [p for p in people if q.lower() in p["name"].lower() or q in p["mrn"]]
        if status == "living":
            people = [p for p in people if not p["deceased"]]
        elif status == "deceased":
            people = [p for p in people if p["deceased"]]
        people.sort(key=lambda p: p["list_name"])
        rows = [[f"<a href='/ehr/patients/{p['id']}/summary'>{_x(p['list_name'])}</a>", p["mrn"], p["dob"],
                 str(p["age"]), p["sex"], _x(p["city"]), p["last_visit"],
                 f"<span class='dec'>Deceased</span>" if p["deceased"] else "Living"] for p in people]
        opts = "".join(f"<option value='{v}'{' selected' if v == status else ''}>{t}</option>"
                       for v, t in (("all", "All patients"), ("living", "Living"), ("deceased", "Deceased")))
        form = (f"<form class='bar' action='/ehr' method='get'><label for='q'>Name or MRN</label>"
                f"<input id='q' name='q' value='{_x(q)}'><select name='status' aria-label='Status'>{opts}</select>"
                f"<button type='submit'>Search</button></form>")
        body = (f"<main><h1>Patient lookup</h1>{form}<p class='muted'>{len(rows)} of {len(chart.patients)} patients</p>"
                + _table(["Patient", "MRN", "DOB", "Age", "Sex", "City", "Last visit", "Status"], rows,
                         numeric={3}, dates={2, 6}, empty="No patients match.") + "</main>")
        return HTMLResponse(_page("Patient lookup", body))

    @r.get("/ehr/patients/{pid}")
    def patient_root(pid: str):
        need(pid)
        return RedirectResponse(f"/ehr/patients/{pid}/summary", status_code=303)

    # ---- chart tabs ----------------------------------------------------------------

    @r.get("/ehr/patients/{pid}/summary", response_class=HTMLResponse)
    def summary(pid: str):
        h = need(pid)
        active, _ = ehr.problems(pid)
        meds, _ = ehr.medications(pid)
        dates, vit = ehr.vitals_flowsheet(pid, 4)
        labs = [l for l in ehr.latest_by_test(pid, {"laboratory"})]
        labs.sort(key=lambda l: l["date"], reverse=True)
        encs = ehr.encounters(pid)[:10]
        plans = [c for c in ehr.careplans(pid) if c["status"] == "Active"]
        dx = [p for p in active if p["kind"] == "Disorder"]
        cards = [
            ("Active problems", _table(["Problem", "Onset"], [[_x(p["name"]), p["onset"]] for p in dx], dates={1})),
            ("Active medications", _table(["Medication", "Since"], [[_x(m["name"]), m["start"]] for m in meds], dates={1})),
            ("Recent vitals", _table(["Measure"] + dates, [[_x(n)] + [_x(v) for v in vals] for n, vals in vit])),
            ("Recent lab results", _table(["Test", "Result", "Date"], [[_x(l["name"]), _x(l["value"]), l["date"]] for l in labs[:15]], dates={2})),
            ("Recent encounters", _table(["Date", "Type", "Reason"], [[f"<a href='/ehr/patients/{pid}/encounters/{e['Id']}'>{_d(e['START'])}</a>",
                                         _x(_clean(e["DESCRIPTION"])), _x(_clean(e["REASONDESCRIPTION"]))] for e in encs], dates={0})),
            ("Active care plans", _table(["Plan", "For", "Since"], [[_x(c["name"]), _x(c["reason"]), c["start"]] for c in plans], dates={2})),
        ]
        grid = "<div class='grid'>" + "".join(f"<div class='card'><h2>{t}</h2>{c}</div>" for t, c in cards) + "</div>"
        return chart_page(h, "summary", "Chart summary", grid)

    @r.get("/ehr/patients/{pid}/problems", response_class=HTMLResponse)
    def problems(pid: str):
        h = need(pid)
        active, resolved = ehr.problems(pid)
        rows = lambda ps, res: [[_x(p["name"]), _x(p["kind"]), p["code"], p["onset"]] + ([p["resolved"]] if res else [])
                                + [str(p["episodes"]), _status(p["active"])] for p in ps]
        content = (f"<h2>Active ({len(active)})</h2>" + _table(["Problem", "Type", "SNOMED", "Onset", "Episodes", "Status"], rows(active, False), numeric={4}, dates={2, 3})
                   + f"<h2>Resolved ({len(resolved)})</h2>" + _table(["Problem", "Type", "SNOMED", "First onset", "Last resolved", "Episodes", "Status"], rows(resolved, True), numeric={5}, dates={2, 3, 4}))
        return chart_page(h, "problems", "Problem list", content)

    @r.get("/ehr/patients/{pid}/medications", response_class=HTMLResponse)
    def medications(pid: str):
        h = need(pid)
        active, past = ehr.medications(pid)
        def rows(ms): return [[_x(m["name"]), m["code"], _x(m["reason"]), m["start"], m["last"], m["stop"], str(m["orders"]), str(m["dispenses"])] for m in ms]
        hdr = ["Medication", "RxNorm", "Indication", "Started", "Last ordered", "Stopped", "Orders", "Dispenses"]
        content = (f"<h2>Active ({len(active)})</h2>" + _table(hdr, rows(active), numeric={6, 7}, dates={1, 3, 4, 5})
                   + f"<h2>Medication history ({len(past)})</h2>" + _table(hdr, rows(past), numeric={6, 7}, dates={1, 3, 4, 5}))
        return chart_page(h, "medications", "Medications", content)

    @r.get("/ehr/patients/{pid}/allergies", response_class=HTMLResponse)
    def allergies(pid: str):
        h = need(pid)
        rows = [[_x(a["name"]), _x(a["type"]), _x(a["category"]), _x(a["reactions"]), a["onset"], _x(a["status"])] for a in ehr.allergies(pid)]
        return chart_page(h, "allergies", "Allergies and intolerances",
                          _table(["Allergen", "Type", "Category", "Reactions (severity)", "Onset", "Status"], rows, dates={4}, empty="No known allergies"))

    @r.get("/ehr/patients/{pid}/vitals", response_class=HTMLResponse)
    def vitals(pid: str):
        h = need(pid)
        dates, vit = ehr.vitals_flowsheet(pid, 12)
        return chart_page(h, "vitals", "Vital signs flowsheet (last 12 dates)",
                          _table(["Measure"] + dates, [[_x(n)] + [_x(v) for v in vals] for n, vals in vit])
                          + f"<p class='muted'>Full history: <a href='/ehr/patients/{pid}/results?category=vital-signs'>all vital sign results</a></p>")

    @r.get("/ehr/patients/{pid}/results", response_class=HTMLResponse)
    def results(pid: str, category: str = "", test: str = "", page: int = Query(1, ge=1)):
        h = need(pid)
        allobs = ehr.observations(pid)
        cats = sorted({o["CATEGORY"] for o in allobs if o["CATEGORY"]})
        tests = sorted({o["DESCRIPTION"] for o in allobs if not category or o["CATEGORY"] == category})
        rows = [o for o in allobs if (not category or o["CATEGORY"] == category) and (not test or o["DESCRIPTION"] == test)]
        shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        copt = "<option value=''>All categories</option>" + "".join(f"<option value='{_x(c)}'{' selected' if c == category else ''}>{_x(c)}</option>" for c in cats)
        topt = "<option value=''>All tests</option>" + "".join(f"<option value='{_x(t)}'{' selected' if t == test else ''}>{_x(t)}</option>" for t in tests)
        form = (f"<form class='bar' method='get'><select name='category' aria-label='Category'>{copt}</select>"
                f"<select name='test' aria-label='Test'>{topt}</select><button type='submit'>Filter</button>"
                f"<a href='/ehr/patients/{pid}/results'>Clear</a></form>")
        extra = (f"&category={_x(category)}" if category else "") + (f"&test={_x(test)}" if test else "")
        base = f"/ehr/patients/{pid}/results"
        body = _table(["Date", "Test", "Result", "Category", "LOINC", "Encounter"],
                      [[_d(o["DATE"]), _x(o["DESCRIPTION"]), _x(_val(o)), _x(o["CATEGORY"]), o["CODE"],
                        f"<a href='/ehr/patients/{pid}/encounters/{o['ENCOUNTER']}'>View</a>" if o["ENCOUNTER"] else ""] for o in shown],
                      dates={0, 4}, empty="No results match.")
        return chart_page(h, "results", "Results review", form + _pager(base, page, len(rows), extra) + body + _pager(base, page, len(rows), extra))

    @r.get("/ehr/patients/{pid}/encounters", response_class=HTMLResponse)
    def encounters(pid: str, cls: str = "", page: int = Query(1, ge=1)):
        h = need(pid)
        allenc = ehr.encounters(pid)
        classes = sorted({e["ENCOUNTERCLASS"] for e in allenc})
        rows = [e for e in allenc if not cls or e["ENCOUNTERCLASS"] == cls]
        shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        opt = "<option value=''>All types</option>" + "".join(f"<option value='{c}'{' selected' if c == cls else ''}>{c}</option>" for c in classes)
        form = f"<form class='bar' method='get'><select name='cls' aria-label='Encounter class'>{opt}</select><button type='submit'>Filter</button></form>"
        base, extra = f"/ehr/patients/{pid}/encounters", (f"&cls={cls}" if cls else "")
        body = _table(["Date", "Class", "Encounter", "Reason", "Total cost", "Payer covered"],
                      [[f"<a href='/ehr/patients/{pid}/encounters/{e['Id']}'>{_d(e['START'])}</a>", _x(e["ENCOUNTERCLASS"]),
                        _x(_clean(e["DESCRIPTION"])), _x(_clean(e["REASONDESCRIPTION"])), _money(e["TOTAL_CLAIM_COST"]),
                        _money(e["PAYER_COVERAGE"])] for e in shown], numeric={4, 5}, dates={0})
        return chart_page(h, "encounters", "Encounters", form + _pager(base, page, len(rows), extra) + body + _pager(base, page, len(rows), extra))

    @r.get("/ehr/patients/{pid}/encounters/{eid}", response_class=HTMLResponse)
    def encounter(pid: str, eid: str):
        h = need(pid)
        d = ehr.encounter(pid, eid)
        if not d:
            raise HTTPException(404, "Unknown encounter")
        e = d["enc"]
        kv = (f"<dl class='kv'><dt>Date</dt><dd>{e['START'].replace('T', ' ')[:16]} to {e['STOP'].replace('T', ' ')[:16]}</dd>"
              f"<dt>Class</dt><dd>{_x(e['ENCOUNTERCLASS'])}</dd><dt>Encounter</dt><dd>{_x(_clean(e['DESCRIPTION']))} (SNOMED {e['CODE']})</dd>"
              f"<dt>Reason</dt><dd>{_x(_clean(e['REASONDESCRIPTION'])) or '—'}</dd>"
              f"<dt>Cost</dt><dd>{_money(e['TOTAL_CLAIM_COST'])} total, {_money(e['PAYER_COVERAGE'])} covered by payer</dd></dl>")
        parts = [
            ("Diagnoses recorded", _table(["Condition", "SNOMED", "Status"], [[_x(_clean(c["DESCRIPTION"])), c["CODE"], "Resolved " + _d(c["STOP"]) if c["STOP"] else "Active"] for c in d["conditions"]])),
            ("Observations", _table(["Test", "Result", "Category"], [[_x(o["DESCRIPTION"]), _x(_val(o)), _x(o["CATEGORY"])] for o in d["observations"]])),
            ("Medications ordered", _table(["Medication", "Indication", "Dispenses"], [[_x(m["DESCRIPTION"]), _x(_clean(m["REASONDESCRIPTION"])), m["DISPENSES"]] for m in d["medications"]], numeric={2})),
            ("Procedures", _table(["Procedure", "Reason"], [[_x(_clean(p["DESCRIPTION"])), _x(_clean(p["REASONDESCRIPTION"]))] for p in d["procedures"]])),
            ("Imaging", _table(["Modality", "Body site", "Series", "Images"], [[_x(s["modality"]), _x(s["site"]), str(len(s["series"])), str(s["instances"])] for s in d["imaging"]], numeric={2, 3})),
            ("Care plans", _table(["Plan", "For"], [[_x(_clean(c["DESCRIPTION"])), _x(_clean(c["REASONDESCRIPTION"]))] for c in d["careplans"]])),
        ]
        content = (f"<p><a href='/ehr/patients/{pid}/encounters'>&larr; All encounters</a></p>{kv}"
                   + "".join(f"<h2>{t}</h2>{c}" for t, c in parts))
        return chart_page(h, "encounters", f"Encounter on {_d(e['START'])}", content)

    @r.get("/ehr/patients/{pid}/procedures", response_class=HTMLResponse)
    def procedures(pid: str, page: int = Query(1, ge=1)):
        h = need(pid)
        rows = ehr.procedures(pid)
        shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        base = f"/ehr/patients/{pid}/procedures"
        body = _table(["Date", "Procedure", "SNOMED", "Reason", "Encounter"],
                      [[_d(p["START"]), _x(_clean(p["DESCRIPTION"])), p["CODE"], _x(_clean(p["REASONDESCRIPTION"])),
                        f"<a href='/ehr/patients/{pid}/encounters/{p['ENCOUNTER']}'>View</a>"] for p in shown], dates={0, 2})
        return chart_page(h, "procedures", "Procedures", _pager(base, page, len(rows)) + body + _pager(base, page, len(rows)))

    @r.get("/ehr/patients/{pid}/imaging", response_class=HTMLResponse)
    def imaging(pid: str):
        h = need(pid)
        rows = [[s["date"], _x(s["modality"]), _x(s["site"]), _x(s["sop"]), str(len(s["series"])), str(s["instances"]),
                 f"<a href='/ehr/patients/{pid}/encounters/{s['encounter']}'>View</a>"] for s in ehr.imaging(pid)]
        return chart_page(h, "imaging", "Imaging studies",
                          _table(["Date", "Modality", "Body site", "Image type", "Series", "Images", "Encounter"], rows, numeric={4, 5}, dates={0}))

    @r.get("/ehr/patients/{pid}/careplans", response_class=HTMLResponse)
    def careplans(pid: str):
        h = need(pid)
        rows = [[_x(c["name"]), _x(c["reason"]), c["start"], c["stop"], c["status"]] for c in ehr.careplans(pid)]
        return chart_page(h, "careplans", "Care plans", _table(["Care plan", "Reason", "Started", "Ended", "Status"], rows, dates={2, 3}))

    @r.get("/ehr/patients/{pid}/demographics", response_class=HTMLResponse)
    def demographics(pid: str):
        h = need(pid)
        p = chart.patients[pid]
        fields = [("Name", " ".join(x for x in (p["PREFIX"], h["name"], p["SUFFIX"]) if x)),
                  ("Maiden name", re.sub(r"\d+", "", p["MAIDEN"])), ("MRN", h["mrn"]), ("Date of birth", p["BIRTHDATE"]),
                  ("Date of death", _d(p["DEATHDATE"])), ("Sex", h["sex"]), ("Marital status", p["MARITAL"]),
                  ("Race", p["RACE"].capitalize()), ("Ethnicity", p["ETHNICITY"].capitalize()), ("Birthplace", p["BIRTHPLACE"]),
                  ("Address", f"{p['ADDRESS']}, {p['CITY']}, {p['STATE']} {p['ZIP']}"), ("County", p["COUNTY"]),
                  ("SSN", p["SSN"]), ("Driver's license", p["DRIVERS"]), ("Passport", p["PASSPORT"]),
                  ("Income", _money(p["INCOME"]) if p["INCOME"] else ""), ("Lifetime healthcare expenses", _money(p["HEALTHCARE_EXPENSES"])),
                  ("Lifetime coverage", _money(p["HEALTHCARE_COVERAGE"])), ("System ID", pid)]
        kv = "<dl class='kv'>" + "".join(f"<dt>{k}</dt><dd>{_x(v) or '—'}</dd>" for k, v in fields) + "</dl>"
        return chart_page(h, "demographics", "Demographics", f"<div class='card'>{kv}</div>")

    # ---- printed chart (for PNG rendering) ---------------------------------------------

    @r.get("/ehr/api/chart-pages")
    def chart_pages() -> list[dict]:
        return [{"id": pid, "mrn": mrn(pid), "name": ehr.ident(pid)["name"], "pages": len(printed(pid))}
                for pid in chart.patients]

    cache: dict[str, list[str]] = {}

    def printed(pid: str) -> list[str]:
        if pid not in cache:
            cache[pid] = _print_pages(ehr, pid)
        return cache[pid]

    @r.get("/ehr/patients/{pid}/chart/{n}", response_class=HTMLResponse)
    def chart_print(pid: str, n: int):
        h = need(pid)
        pages = printed(pid)
        if not 1 <= n <= len(pages):
            raise HTTPException(404, "No such page")
        return HTMLResponse(_print_html(ehr, h, pages[n - 1], n, len(pages)))

    return r


# ---- printed chart --------------------------------------------------------------------

def _print_sections(ehr: EHR, pid: str) -> list[tuple[str, list[tuple[str, int]], list[list[str]]]]:
    """(title, [(column, width px)], rows). Widths add up to the page's content width."""
    active, resolved = ehr.problems(pid)
    meds, past = ehr.medications(pid)
    dates, vit = ehr.vitals_flowsheet(pid, 6)
    allg = ehr.allergies(pid)
    labs = ehr.latest_by_test(pid, {"laboratory"})
    other = ehr.latest_by_test(pid, {"survey", "social-history", "exam", "therapy", "imaging", "procedure"})
    W = CHART_W - 48
    vw = (W - 330) // max(1, len(dates)) if dates else 0
    return [
        ("Allergies and intolerances", [("Allergen", 300), ("Category", 140), ("Reactions (severity)", W - 300 - 140 - 110 - 140), ("Onset", 110), ("Status", 140)],
         [[a["name"], a["category"], a["reactions"], a["onset"], a["status"]] for a in allg] or [["No known allergies", "", "", "", ""]]),
        (f"Active problems ({len(active)})", [("Problem", W - 110 - 110 - 70), ("Type", 110), ("Onset", 110), ("Episodes", 70)],
         [[p["name"], p["kind"], p["onset"], str(p["episodes"])] for p in active]),
        (f"Active medications ({len(meds)})", [("Medication", W - 300 - 110 - 110), ("Indication", 300), ("Started", 110), ("Last ordered", 110)],
         [[m["name"], m["reason"], m["start"], m["last"]] for m in meds]),
        ("Vital signs (most recent dates)", [("Measure", 330)] + [(d, vw) for d in dates],
         [[n] + vals for n, vals in vit]),
        (f"Laboratory results, latest per test ({len(labs)})", [("Test", W - 170 - 110 - 270 - 60), ("Latest", 170), ("Date", 110), ("Previous (date)", 270), ("Count", 60)],
         [[l["name"], l["value"], l["date"], l["prior"], str(l["count"])] for l in labs]),
        (f"Assessments and other observations, latest ({len(other)})", [("Observation", W - 330 - 110 - 60), ("Latest", 330), ("Date", 110), ("Count", 60)],
         [[l["name"], l["value"], l["date"], str(l["count"])] for l in other]),
        (f"Resolved problems ({len(resolved)})", [("Problem", W - 110 - 110 - 110 - 70), ("Type", 110), ("First onset", 110), ("Last resolved", 110), ("Episodes", 70)],
         [[p["name"], p["kind"], p["onset"], p["resolved"], str(p["episodes"])] for p in resolved]),
        (f"Medication history ({len(past)})", [("Medication", W - 280 - 110 - 110 - 70), ("Indication", 280), ("Started", 110), ("Stopped", 110), ("Orders", 70)],
         [[m["name"], m["reason"], m["start"], m["stop"], str(m["orders"])] for m in past]),
        ("Care plans", [("Care plan", W - 340 - 110 - 110 - 100), ("Reason", 340), ("Started", 110), ("Ended", 110), ("Status", 100)],
         [[c["name"], c["reason"], c["start"], c["stop"], c["status"]] for c in ehr.careplans(pid)]),
        ("Imaging studies", [("Date", 110), ("Modality", 220), ("Body site", W - 110 - 220 - 260 - 70 - 70), ("Image type", 260), ("Series", 70), ("Images", 70)],
         [[s["date"], s["modality"], s["site"], s["sop"], str(len(s["series"])), str(s["instances"])] for s in ehr.imaging(pid)]),
        ("Procedures (grouped)", [("Procedure", W - 300 - 60 - 110 - 110), ("Reason", 300), ("Count", 60), ("First", 110), ("Last", 110)],
         [[p["name"], p["reason"], str(p["n"]), p["first"], p["last"]] for p in ehr.procedures_grouped(pid)]),
        (f"Encounters ({len(ehr.encounters(pid))})", [("Date", 110), ("Class", 110), ("Encounter", W - 110 - 110 - 330 - 110), ("Reason", 330), ("Cost", 110)],
         [[_d(e["START"]), e["ENCOUNTERCLASS"], _clean(e["DESCRIPTION"]), _clean(e["REASONDESCRIPTION"]), _money(e["TOTAL_CLAIM_COST"])] for e in ehr.encounters(pid)]),
    ]


def _row_px(row: list[str], cols: list[tuple[str, int]]) -> int:
    lines = max(max(1, math.ceil(len(c) * CHAR_PX / max(1, w - 14))) for c, (_, w) in zip(row, cols))
    return lines * LINE_PX + ROW_PAD_PX


def _print_pages(ehr: EHR, pid: str) -> list[str]:
    """Lays the sections out into fixed-height pages, splitting long tables with '(continued)'."""
    pages: list[str] = []
    cur: list[str] = []
    used = 0

    def flush():
        nonlocal cur, used
        pages.append("".join(cur))
        cur, used = [], 0

    for title, cols, rows in _print_sections(ehr, pid):
        if not rows:
            continue
        i, cont = 0, False
        while i < len(rows):
            if used + HEAD_PX + _row_px(rows[i], cols) > CHART_BODY_PX:
                flush()
            chunk, h = [], HEAD_PX
            while i < len(rows) and used + h + _row_px(rows[i], cols) <= CHART_BODY_PX:
                h += _row_px(rows[i], cols)
                chunk.append(rows[i])
                i += 1
            if not chunk:  # a single row taller than a page: place it anyway
                chunk, h, i = [rows[i]], h + _row_px(rows[i], cols), i + 1
            colgroup = "".join(f"<col style='width:{w}px'>" for _, w in cols)
            head = "".join(f"<th>{_x(c)}</th>" for c, _ in cols)
            body = "".join("<tr>" + "".join(f"<td>{_x(c)}</td>" for c in r) + "</tr>" for r in chunk)
            cur.append(f"<section><h2>{_x(title)}{' (continued)' if cont else ''}</h2><table><colgroup>{colgroup}</colgroup>"
                       f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></section>")
            used += h + GAP_PX
            cont = True
    if cur or not pages:
        flush()
    return pages


PRINT_CSS = f"""
*{{box-sizing:border-box}} html,body{{margin:0;background:#fff}}
body{{width:{CHART_W}px;height:{CHART_H}px;overflow:hidden;font:13px/{LINE_PX}px Inter,Arial,sans-serif;color:#111;position:relative}}
.hd{{background:#1f3a5f;color:#fff;padding:10px 24px;display:flex;justify-content:space-between;align-items:baseline}}
.hd b{{font-size:15px}} .pt{{padding:10px 24px 8px;border-bottom:2px solid #1f3a5f}}
.pt .nm{{font-size:21px;font-weight:700;line-height:26px}} .pt .f{{display:flex;flex-wrap:wrap;gap:4px 22px;margin-top:4px}}
.pt .f span{{color:#555}} .pt .f b{{color:#111}} .dec{{color:#fff;background:#b3261e;padding:0 6px;border-radius:3px;font-weight:700;margin-left:10px;font-size:13px}}
.bd{{padding:8px 24px 0}} section{{margin-bottom:{GAP_PX}px}}
h2{{font-size:13px;line-height:22px;height:30px;margin:0;padding-top:4px;text-transform:uppercase;letter-spacing:.04em;color:#1f3a5f}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}}
th{{background:#e8edf3;text-align:left;padding:0 7px;height:26px;font-weight:600;border:1px solid #c9d1dc;white-space:nowrap;overflow:hidden}}
td{{padding:3px 7px;border:1px solid #d6dce4;vertical-align:top;overflow-wrap:anywhere}}
.ft{{position:absolute;left:24px;right:24px;bottom:12px;display:flex;justify-content:space-between;color:#555;font-size:12px;border-top:1px solid #ccc;padding-top:6px}}
"""


def _print_html(ehr: EHR, h: dict, content: str, n: int, total: int) -> str:
    p = ehr.c.patients[h["id"]]
    alg = ehr.allergy_names(h["id"])
    dec = f"<span class='dec'>DECEASED {_x(h['deceased'])}</span>" if h["deceased"] else ""
    fields = [("MRN", h["mrn"]), ("DOB", f"{h['dob']} ({h['age']} y)"), ("Sex", h["sex"]),
              ("Address", f"{p['ADDRESS']}, {p['CITY']}, {p['STATE']} {p['ZIP']}"),
              ("Allergies", ", ".join(alg) or "No known allergies")]
    f = "".join(f"<span>{k}: <b>{_x(v)}</b></span>" for k, v in fields)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Chart {h['mrn']} p{n}</title><style>{PRINT_CSS}</style></head><body>"
            f"<div class='hd'><b>{PRODUCT} · Patient chart</b><span>Printed {ehr.c.today.isoformat()} · Synthetic record</span></div>"
            f"<div class='pt'><div class='nm'>{_x(h['name'])}{dec}</div><div class='f'>{f}</div></div>"
            f"<div class='bd'>{content}</div>"
            f"<div class='ft'><span>{_x(h['name'])} · MRN {h['mrn']}</span><span>Page {n} of {total}</span></div></body></html>")
