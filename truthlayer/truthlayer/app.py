"""HTTP API + UI. Any downstream app (referral intake, state reporting, compliance alerts)
can consume these endpoints without the UI."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from .engine import Engine, resolve_issue
from .store import Store
from . import views

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("TRUTHLAYER_CONFIG", ROOT / "clients" / "harborview.yaml"))
DB = os.environ.get("TRUTHLAYER_DB", str(ROOT / "truthlayer.db"))
SAMPLE = Path(os.environ.get("TRUTHLAYER_SAMPLE", ROOT / "sample_data"))

cfg = yaml.safe_load(CONFIG.read_text())
store = Store(DB)
eng = Engine(cfg, store)
app = FastAPI(title="TruthLayer", version="1.0")


def _issue_out(i):
    i = dict(i)
    i["data"] = json.loads(i.pop("data_json"))
    ent = store.one("SELECT display_name FROM entities WHERE key=?", (i["entity_key"],)) if i["entity_key"] else None
    i["entity_name"] = ent["display_name"] if ent else None
    return i


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/summary")
def summary():
    return eng.summary()


@app.get("/api/config")
def config():
    return {"yaml": CONFIG.read_text(), "parsed": cfg, "file": CONFIG.name}


@app.post("/api/files")
async def upload(files: list[UploadFile] = File(...), source: str | None = Form(None)):
    out = []
    for f in files:
        try:
            out.append({"filename": f.filename, "ok": True, **eng.ingest(await f.read(), f.filename, source or None)})
        except Exception as e:  # report, don't crash the demo
            store.event("ingest_error", f"Could not ingest {f.filename}: {e}")
            out.append({"filename": f.filename, "ok": False, "error": str(e)})
    return out


@app.get("/api/files")
def list_files():
    rows = store.q("SELECT id, source, filename, uploaded_at, active, row_count, notes_json FROM files ORDER BY id DESC")
    for r in rows:
        r["notes"] = json.loads(r.pop("notes_json") or "{}")
        r["label"] = eng.label(r["source"])
    return rows


@app.delete("/api/files/{fid}")
def deactivate(fid: int):
    store.x("UPDATE files SET active=0 WHERE id=?", (fid,))
    return eng.process()


@app.post("/api/process")
def process():
    return eng.process()


@app.post("/api/load-sample")
def load_sample():
    for p in sorted(SAMPLE.iterdir()):
        if p.suffix.lower() in (".csv", ".pdf", ".xlsx", ".tsv"):
            eng.ingest(p.read_bytes(), p.name)
    return eng.process()


@app.post("/api/reset")
def reset():
    store.reset()
    return {"ok": True}


class AsOf(BaseModel):
    as_of: str


@app.put("/api/settings")
def settings(body: AsOf):
    store.set_setting("as_of", body.as_of)
    return eng.process() if store.one("SELECT 1 AS x FROM records LIMIT 1") else eng.summary()


@app.get("/api/entities")
def entities():
    rows = store.q("SELECT * FROM entities ORDER BY status DESC, key")
    for r in rows:
        r["attrs"] = json.loads(r.pop("attrs_json"))
        r["sources"] = json.loads(r.pop("sources_json"))
    return rows


@app.get("/api/entities/{key}")
def entity(key: str):
    e = store.one("SELECT * FROM entities WHERE key=?", (key,))
    if not e:
        raise HTTPException(404)
    e["attrs"] = json.loads(e.pop("attrs_json"))
    e["sources"] = json.loads(e.pop("sources_json"))
    recs = store.q("""SELECT r.id, r.source, r.locator, r.raw_json, r.norm_json, r.match_method, r.match_score, r.match_status,
                      f.filename, f.uploaded_at FROM records r JOIN files f ON f.id=r.file_id
                      WHERE f.active=1 AND r.entity_key=? ORDER BY r.source, r.id""", (key,))
    for r in recs:
        r["raw"] = {k: v for k, v in json.loads(r.pop("raw_json")).items() if not k.startswith("_")}
        r["norm"] = {k: v for k, v in json.loads(r.pop("norm_json") or "{}").items() if not k.startswith("_")}
        r["label"] = eng.label(r["source"])
    issues = [_issue_out(i) for i in store.q(
        "SELECT * FROM issues WHERE active=1 AND (entity_key=? OR data_json LIKE ?) ORDER BY status='open' DESC, severity", (key, f'%"{key}"%'))]
    events = store.q("SELECT * FROM events WHERE entity_key=? ORDER BY id", (key,))
    files = {}
    for r in recs:
        files.setdefault(r["filename"], {"at": r["uploaded_at"], "kind": "ingest", "message": f"{r['label']} record(s) received from {r['filename']}"})
    timeline = sorted(list(files.values()) + events, key=lambda x: x["at"])
    return {"entity": e, "records": recs, "issues": issues, "timeline": timeline,
            "authority": {a: s.get("authority", []) for a, s in cfg["attributes"].items()},
            "labels": {k: v.get("label", k) for k, v in cfg["sources"].items()}}


@app.get("/api/issues")
def issues(status: str | None = None, severity: str | None = None, category: str | None = None):
    sql, args = "SELECT * FROM issues WHERE active=1", []
    for col, val in (("status", status), ("severity", severity), ("category", category)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, category, id"
    return [_issue_out(i) for i in store.q(sql, args)]


class Decision(BaseModel):
    decision: str
    value: str | None = None
    note: str | None = None
    reviewer: str | None = None


@app.post("/api/issues/{iid}/resolve")
def resolve(iid: int, d: Decision):
    try:
        return resolve_issue(eng, iid, d.decision, d.value, d.note, d.reviewer)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/events")
def events(limit: int = 200):
    return store.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/views/{name}")
def view(name: str):
    fn = {"staffing": views.staffing, "credentials": views.credentials, "coverage": views.coverage}.get(name)
    if not fn:
        raise HTTPException(404)
    return fn(eng)


@app.get("/api/export/{name}.csv")
def export(name: str):
    if name == "staffing":
        rows = views.staffing(eng)["rows"]
        cols = ["period_start", "period_end", "facility", "role", "headcount", "paid_hours", "scheduled_hours", "flagged_hours", "confidence"]
    elif name == "issues":
        rows = issues()
        cols = ["id", "severity", "category", "status", "entity_key", "entity_name", "attribute", "title", "detail", "action"]
    elif name == "entities":
        rows = []
        for e in entities():
            row = {"key": e["key"], "name": e["display_name"], "status": e["status"], "open_issues": e["open_issues"]}
            for a, d in e["attrs"].items():
                row[a] = d["value"]
                row[a + "_status"] = d["status"]
            rows.append(row)
        cols = list(rows[0].keys()) if rows else []
    else:
        raise HTTPException(404)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={name}.csv"})
