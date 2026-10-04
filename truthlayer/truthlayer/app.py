"""HTTP API + UI host.

/api/workspaces                 list / create client workspaces (one per company)
/api/w/{ws}/setup/...           guided setup: drop CSVs -> suggested setup -> review -> save
/api/w/{ws}/...                 everything else, scoped to one company

Any downstream app (referral intake, state reporting, alerts) can call these endpoints without the UI.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from . import views, workspaces as W, sqlviews
from .engine import resolve_issue, similar_options, create_auto_rule
from .setup import analyze, build_config

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="TruthLayer", version="2.0")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


# ------------------------------------------------------------------ workspaces
class NewWorkspace(BaseModel):
    name: str = ""
    industry: str = ""
    preset: str | None = None


@app.get("/api/workspaces")
def list_workspaces():
    return {"workspaces": W.list_all(), "presets": [{"id": k, "empty": bool(v.get("empty")), **{x: v[x] for x in ("name", "industry", "blurb")}} for k, v in W.PRESETS.items()]}


@app.post("/api/workspaces")
def create_workspace(body: NewWorkspace):
    if body.preset:
        p = W.PRESETS.get(body.preset)
        if not p:
            raise HTTPException(404, "unknown preset")
        ws = W.create(body.name or p["name"], p["industry"], body.preset)
        if not p.get("sample"):          # the setup only: the real files get dropped in afterwards
            ws.save_config(yaml.safe_load((ROOT / p["config"]).read_text()))
            return ws.info()
        ws.store.set_setting("as_of", "2026-10-04")   # the sample data is dated around this day
        sample = ROOT / p["sample"]
        files = [(f.name, f.read_bytes()) for f in sorted(sample.iterdir()) if f.suffix.lower() in (".csv", ".xlsx", ".pdf", ".tsv")]
        if p["config"]:
            cfg = yaml.safe_load((ROOT / p["config"]).read_text())
            ws.save_config(cfg)
            for name, data in files:
                ws.keep_upload(name, data)
                ws.engine.ingest(data, name)
        else:   # set the company up automatically, exactly like the guided setup would
            res = analyze(files)
            ws.save_config(build_config(ws.meta["name"], res["profiles"], res["choices"]))
            for p_ in res["profiles"]:
                data = next(d for n, d in files if n == p_["filename"])
                ws.keep_upload(p_["filename"], data)
                ws.engine.ingest(data, p_["filename"], forced=p_["sid"])
        ws.process()
        return ws.info()
    ws = W.create(body.name or "New company", body.industry)
    return ws.info()


class Rename(BaseModel):
    name: str


@app.patch("/api/workspaces/{slug}")
def rename_workspace(slug: str, body: Rename):
    W.rename(slug, body.name)
    return W.get(slug).info()


@app.delete("/api/workspaces/{slug}")
def delete_workspace(slug: str):
    W.remove(slug)
    return {"ok": True}


# ------------------------------------------------------------------ everything scoped to one company
def ws_dep(ws: str):
    try:
        return W.get(ws)
    except KeyError:
        raise HTTPException(404, "No such company")


def ready(ws=Depends(ws_dep)):
    if not ws.configured:
        raise HTTPException(409, "This company hasn't been set up yet")
    return ws


r = APIRouter(prefix="/api/w/{ws}")


@r.get("/info")
def info(ws=Depends(ws_dep)):
    return ws.info()


# ---- guided setup
def _public(res):
    strip = lambda p: {**p, "names": None, "columns": [{k: v for k, v in c.items() if k not in ("values", "all_values")} for c in p["columns"]]}
    return {"profiles": [strip(p) for p in res.get("profiles", [])], "choices": res.get("choices"), "skipped": res.get("skipped", [])}


def _analyze_staging(ws, master=None):
    st = ws.staging()
    files = [(f.name, f.read_bytes()) for f in sorted(st.iterdir()) if f.is_file() and not f.name.startswith("_")]
    res = analyze(files, master)
    (st / "_analysis.json").write_text(json.dumps(res))
    return res


@r.get("/setup")
def setup_state(ws=Depends(ws_dep)):
    p = ws.staging() / "_analysis.json"
    return _public(json.loads(p.read_text())) if p.exists() else {"profiles": [], "choices": None, "skipped": []}


@r.post("/setup/files")
async def setup_files(files: list[UploadFile] = File(...), ws=Depends(ws_dep)):
    st = ws.staging()
    for f in files:
        (st / Path(f.filename).name).write_bytes(await f.read())
    return _public(_analyze_staging(ws))


@r.post("/setup/from-current")
def setup_from_current(ws=Depends(ready)):
    """Redo the setup using the files this company already has."""
    st = ws.staging()
    for f in st.iterdir():
        f.unlink()
    active = {f["filename"] for f in ws.store.q("SELECT filename FROM files WHERE active=1")}
    for up in sorted((ws.dir / "uploads").glob("*")):
        name = up.name.split("_", 1)[1]
        if name in active:
            shutil.copy(up, st / name)
    return _public(_analyze_staging(ws))


class Master(BaseModel):
    master: str


@r.post("/setup/master")
def setup_master(body: Master, ws=Depends(ws_dep)):
    """Re-run the suggestions around a different master file."""
    return _public(_analyze_staging(ws, body.master))


@r.post("/setup/remove/{filename}")
def setup_remove(filename: str, ws=Depends(ws_dep)):
    p = ws.staging() / Path(filename).name
    if p.exists():
        p.unlink()
    return _public(_analyze_staging(ws))


class SaveSetup(BaseModel):
    choices: dict


@r.post("/setup/save")
def setup_save(body: SaveSetup, ws=Depends(ws_dep)):
    st = ws.staging()
    res = json.loads((st / "_analysis.json").read_text())
    cfg = build_config(ws.meta["name"], res["profiles"], body.choices)
    first_time = not ws.configured
    ws.save_config(cfg)
    if not first_time:   # new shape: start the data over, keep the decisions and history
        with ws.store.lock:
            ws.store.db.execute("UPDATE files SET active=0")
            ws.store.db.commit()
    for p in res["profiles"]:
        if not body.choices["sources"].get(p["sid"], {}).get("include", True):
            continue
        data = (st / p["filename"]).read_bytes()
        ws.keep_upload(p["filename"], data)
        ws.engine.ingest(data, p["filename"], forced=p["sid"])
    ws.store.event("setup", f"Setup saved: {len(cfg['sources'])} systems, {len(cfg['attributes'])} compared fields, {len(cfg['rules'])} checks")
    for f in st.iterdir():
        f.unlink()
    return ws.process()


# ---- data
@r.get("/summary")
def summary(ws=Depends(ready)):
    return ws.engine.summary()


@r.get("/config")
def config(ws=Depends(ready)):
    return {"yaml": ws.config_text(), "parsed": ws.cfg, "file": "config.yaml"}


@r.post("/files")
async def upload(files: list[UploadFile] = File(...), source: str | None = Form(None), ws=Depends(ready)):
    out = []
    for f in files:
        data = await f.read()
        try:
            res = ws.engine.ingest(data, f.filename, source or None)
            ws.keep_upload(f.filename, data)
            out.append({"filename": f.filename, "ok": True, **res})
        except Exception as e:
            ws.store.event("ingest_error", f"Could not ingest {f.filename}: {e}")
            out.append({"filename": f.filename, "ok": False, "error": str(e)})
    return out


@r.get("/files")
def list_files(ws=Depends(ready)):
    rows = ws.store.q("SELECT id, source, filename, uploaded_at, active, row_count, notes_json FROM files ORDER BY id DESC")
    for x in rows:
        x["notes"] = json.loads(x.pop("notes_json") or "{}")
        x["label"] = ws.engine.label(x["source"])
    return rows


@r.delete("/files/{fid}")
def deactivate(fid: int, ws=Depends(ready)):
    ws.store.x("UPDATE files SET active=0 WHERE id=?", (fid,))
    return ws.process()


@r.post("/process")
def process(ws=Depends(ready)):
    return ws.process()


@r.post("/reset")
def reset(ws=Depends(ready)):
    ws.store.reset()
    return {"ok": True}


class AsOf(BaseModel):
    as_of: str


@r.put("/settings")
def settings(body: AsOf, ws=Depends(ready)):
    ws.store.set_setting("as_of", body.as_of)
    return ws.process() if ws.store.one("SELECT 1 AS x FROM records LIMIT 1") else ws.engine.summary()


@r.get("/entities")
def entities(ws=Depends(ready)):
    rows = ws.store.q("SELECT * FROM entities ORDER BY status DESC, key")
    for x in rows:
        x["attrs"] = json.loads(x.pop("attrs_json"))
        x["sources"] = json.loads(x.pop("sources_json"))
    return rows


def _issue_out(ws, i):
    i = dict(i)
    i["data"] = json.loads(i.pop("data_json"))
    ent = ws.store.one("SELECT display_name FROM entities WHERE key=?", (i["entity_key"],)) if i["entity_key"] else None
    i["entity_name"] = ent["display_name"] if ent else None
    return i


@r.get("/entities/{key}")
def entity(key: str, ws=Depends(ready)):
    st = ws.store
    e = st.one("SELECT * FROM entities WHERE key=?", (key,))
    if not e:
        raise HTTPException(404)
    e["attrs"] = json.loads(e.pop("attrs_json"))
    e["sources"] = json.loads(e.pop("sources_json"))
    recs = st.q("""SELECT r.id, r.source, r.locator, r.raw_json, r.norm_json, r.match_method, r.match_score, r.match_status,
                   f.filename, f.uploaded_at FROM records r JOIN files f ON f.id=r.file_id
                   WHERE f.active=1 AND r.entity_key=? ORDER BY r.source, r.id""", (key,))
    for x in recs:
        x["raw"] = {k: v for k, v in json.loads(x.pop("raw_json")).items() if not k.startswith("_")}
        x["norm"] = {k: v for k, v in json.loads(x.pop("norm_json") or "{}").items() if not k.startswith("_")}
        x["label"] = ws.engine.label(x["source"])
    issues = [_issue_out(ws, i) for i in st.q(
        "SELECT * FROM issues WHERE active=1 AND (entity_key=? OR data_json LIKE ?) ORDER BY status='open' DESC, severity", (key, f'%"{key}"%'))]
    events = st.q("SELECT * FROM events WHERE entity_key=? ORDER BY id", (key,))
    files = {}
    for x in recs:
        files.setdefault(x["filename"], {"at": x["uploaded_at"], "kind": "ingest", "message": f"{x['label']} record(s) received from {x['filename']}"})
    timeline = sorted(list(files.values()) + events, key=lambda x: x["at"])
    return {"entity": e, "records": recs, "issues": issues, "timeline": timeline}


@r.get("/issues")
def issues(status: str | None = None, severity: str | None = None, category: str | None = None, ws=Depends(ready)):
    sql, args = "SELECT * FROM issues WHERE active=1", []
    for col, val in (("status", status), ("severity", severity), ("category", category)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, category, id"
    return [_issue_out(ws, i) for i in ws.store.q(sql, args)]


class Decision(BaseModel):
    decision: str
    value: str | None = None
    note: str | None = None
    reviewer: str | None = None


@r.post("/issues/{iid}/resolve")
def resolve(iid: int, d: Decision, ws=Depends(ready)):
    try:
        out = resolve_issue(ws.engine, iid, d.decision, d.value, d.note, d.reviewer)
        sqlviews.build(ws.store, ws.cfg)
        return out
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))


@r.get("/issues/{iid}/similar")
def similar(iid: int, ws=Depends(ready)):
    try:
        return similar_options(ws.engine, iid)
    except KeyError:
        raise HTTPException(404)


class NewRule(BaseModel):
    issue_id: int
    condition: dict | None = None
    action: str = "dismiss"
    reviewer: str | None = None


@r.post("/auto-rules")
def new_rule(body: NewRule, ws=Depends(ready)):
    try:
        out = create_auto_rule(ws.engine, body.issue_id, body.condition, body.action, body.reviewer)
        sqlviews.build(ws.store, ws.cfg)
        return out
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))


@r.get("/auto-rules")
def list_rules(ws=Depends(ready)):
    return ws.store.q("SELECT * FROM auto_rules ORDER BY id DESC")


@r.delete("/auto-rules/{rid}")
def delete_rule(rid: int, ws=Depends(ready)):
    rule = ws.store.one("SELECT label FROM auto_rules WHERE id=?", (rid,))
    ws.store.x("UPDATE auto_rules SET active=0 WHERE id=?", (rid,))
    if rule:
        ws.store.event("rule", f"Rule switched off: {rule['label']}")
    return ws.process()


@r.get("/events")
def events(limit: int = 200, ws=Depends(ws_dep)):
    return ws.store.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


VIEWS = {"staffing": views.staffing, "credentials": views.credentials, "coverage": views.coverage,
         "expiring": views.expiring, "totals": views.totals}


@r.get("/views/{name}")
def view(name: str, ws=Depends(ready)):
    fn = VIEWS.get(name)
    if not fn:
        raise HTTPException(404)
    return fn(ws.engine)


@r.get("/tables")
def tables(ws=Depends(ready)):
    return sqlviews.list_tables(ws.store)


@r.get("/tables/{name}")
def table_rows(name: str, limit: int = 200, ws=Depends(ready)):
    try:
        return sqlviews.rows(ws.store, name, limit)
    except KeyError:
        raise HTTPException(404)


@r.get("/export/{name}.csv")
def export(name: str, ws=Depends(ready)):
    if name == "staffing" and ws.cfg.get("views", {}).get("staffing"):
        rows = views.staffing(ws.engine)["rows"]
        cols = ["period_start", "period_end", "facility", "role", "headcount", "paid_hours", "scheduled_hours", "flagged_hours", "confidence"]
    elif name == "issues":
        rows = issues(ws=ws)
        cols = ["id", "severity", "category", "status", "entity_key", "entity_name", "attribute", "title", "detail", "action"]
    elif name == "entities":
        rows = []
        for e in entities(ws=ws):
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
                             headers={"Content-Disposition": f"attachment; filename={ws.slug}-{name}.csv"})


app.include_router(r)
