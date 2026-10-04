"""Business views that consume the source of truth. They never read the raw files:
everything comes from canonical entities, linked records and open issues, so each number
can be traced back and carries the uncertainty attached to it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta


def _load(eng):
    st = eng.store
    ents = {e["key"]: {**e, "attrs": json.loads(e["attrs_json"])} for e in st.q("SELECT * FROM entities")}
    recs = [{**r, "norm": json.loads(r["norm_json"] or "{}"), "raw": json.loads(r["raw_json"])}
            for r in st.q("""SELECT r.*, f.filename FROM records r JOIN files f ON f.id=r.file_id WHERE f.active=1""")]
    issues = [{**i, "data": json.loads(i["data_json"])} for i in st.q("SELECT * FROM issues WHERE active=1 AND status='open'")]
    return ents, recs, issues


def _d(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _val(ent, attr):
    return (ent or {}).get("attrs", {}).get(attr, {}).get("value")


# ------------------------------------------------------------------ staffing report
def staffing(eng):
    v = eng.cfg.get("views", {}).get("staffing")
    if not v:
        return None
    ents, recs, issues = _load(eng)
    src, hrs, s_f, e_f = v["source"], v["hours"], v["start"], v["end"]
    rec_group = {g: eng.cfg["attributes"][g]["from"].get(src) for g in v["group"]}
    sched = eng.cfg.get("views", {}).get("coverage", {}).get("source")
    sched_cfg = eng.cfg["attributes"]

    # hours are "flagged" when an open issue casts doubt on them:
    #   - the issue cites this exact record as evidence (bad value, duplicate row, paid≠scheduled...)
    #   - the issue is about this person's identity, or their role/facility is in conflict
    by_record, by_entity = defaultdict(list), defaultdict(list)
    for i in issues:
        if i["rule"] != "conflict":   # conflicts are handled per attribute below
            for ev in i["data"].get("evidence", []):
                if ev.get("record_id"):
                    by_record[ev["record_id"]].append(i["title"])
        if i["rule"] in ("not_in_anchor", "duplicate_entity", "duplicate_key") or (i["rule"] == "conflict" and i["attribute"] in v["group"]):
            by_entity[i["entity_key"]].append(i["title"])
            for k in i["data"].get("related", []):
                by_entity[k].append(i["title"])

    rows = defaultdict(lambda: {"paid": 0.0, "flagged": 0.0, "staff": set(), "scheduled": 0.0, "reasons": set()})
    periods = set()
    for r in recs:
        if r["source"] != src:
            continue
        s, e = r["norm"].get(s_f), r["norm"].get(e_f)
        h = r["norm"].get(hrs) or 0
        if not s or not e:
            continue
        ent = ents.get(r["entity_key"])
        role = _val(ent, "role") if ent and ent["status"] == "verified" else r["norm"].get(rec_group["role"])
        fac = r["norm"].get(rec_group["facility"]) or _val(ent, "facility")
        k = (s, e, fac or "UNKNOWN", role or "UNKNOWN")
        periods.add((s, e))
        row = rows[k]
        row["paid"] += h
        row["staff"].add(r["entity_key"])
        reasons = []
        if r["match_status"] not in ("auto", "confirmed", "anchor"):
            reasons.append("record not linked to a verified employee")
        reasons += by_record.get(r["id"], []) + by_entity.get(r["entity_key"], [])
        if reasons:
            row["flagged"] += h
            row["reasons"].update(reasons)
    # scheduled hours for periods the schedule fully covers
    if sched:
        sdates = {r["norm"].get("date") for r in recs if r["source"] == sched}
        for (s, e) in periods:
            sd, ed = _d(s), _d(e)
            if not all((sd + timedelta(i)).isoformat() in sdates for i in range((ed - sd).days + 1)):
                continue
            for r in recs:
                if r["source"] != sched or not r["norm"].get("hours"):
                    continue
                d = r["norm"].get("date")
                if s <= d <= e:
                    ent = ents.get(r["entity_key"])
                    role = _val(ent, "role") if ent and ent["status"] == "verified" else r["norm"].get("role")
                    k = (s, e, r["norm"].get("facility") or "UNKNOWN", role or "UNKNOWN")
                    rows[k]["scheduled"] += r["norm"]["hours"]
                    rows[k].setdefault("has_schedule", True)
    out = []
    for (s, e, fac, role), row in sorted(rows.items()):
        out.append({"period_start": s, "period_end": e, "facility": fac, "role": role,
                    "paid_hours": round(row["paid"], 2), "scheduled_hours": round(row["scheduled"], 2) if row.get("has_schedule") or row["scheduled"] else None,
                    "headcount": len(row["staff"]), "flagged_hours": round(row["flagged"], 2),
                    "confidence": round(100 * (1 - row["flagged"] / row["paid"])) if row["paid"] else None,
                    "reasons": sorted(row["reasons"])[:6]})
    total = sum(r["paid_hours"] for r in out)
    flagged = sum(r["flagged_hours"] for r in out)
    return {"rows": out, "total_paid": total, "total_flagged": flagged,
            "report_ready_pct": round(100 * (1 - flagged / total)) if total else None}


# ------------------------------------------------------------------ credentials
def credentials(eng):
    v = eng.cfg.get("views", {}).get("credentials")
    if not v:
        return None
    ents, recs, issues = _load(eng)
    today = eng.as_of()
    warn = next((r.get("warn_days", 60) for r in eng.cfg["rules"] if r["type"] == "expiration"), 60)
    stale = next((r.get("max_age_days", 180) for r in eng.cfg["rules"] if r["type"] == "staleness"), 180)
    open_cred = defaultdict(list)
    for i in issues:
        if i["category"] == "Credentials":
            open_cred[i["entity_key"]].append(i["title"])
    out = []
    for k, e in ents.items():
        num = e["attrs"].get(v["attribute_number"], {})
        exp = e["attrs"].get(v["attribute_expiry"], {})
        ver = e["attrs"].get(v["attribute_verified"], {})
        if num.get("status") == "MISSING" and not open_cred.get(k):
            continue
        d = _d(exp.get("value"))
        vd = _d(ver.get("value"))
        days = (d - today).days if d else None
        if num.get("status") == "MISSING":
            status = "missing"
        elif days is not None and days < 0:
            status = "expired"
        elif num.get("status") == "CONFLICT" or exp.get("status") == "CONFLICT":
            status = "conflict"
        elif ver.get("status") == "MISSING":
            status = "unverified"
        elif days is not None and days <= warn:
            status = "expiring"
        elif vd and (today - vd).days > stale:
            status = "stale"
        else:
            status = "valid"
        out.append({"entity_key": k, "name": e["display_name"], "entity_status": e["status"], "role": _val(e, "role"),
                    "facility": _val(e, "facility"), "license_number": num.get("value"), "expiration": exp.get("value"),
                    "expiration_sources": exp.get("sources"), "days_left": days, "last_verified": ver.get("value"),
                    "verified_age": (today - vd).days if vd else None, "status": status, "issues": open_cred.get(k, [])})
    order = {"expired": 0, "conflict": 1, "missing": 2, "unverified": 3, "expiring": 4, "stale": 5, "valid": 6}
    out.sort(key=lambda r: (order[r["status"]], r["days_left"] if r["days_left"] is not None else 9999))
    counts = defaultdict(int)
    for r in out:
        counts[r["status"]] += 1
    return {"rows": out, "counts": counts, "as_of": today.isoformat(), "warn_days": warn}


# ------------------------------------------------------------------ coverage / referral readiness
BANDS = [("Day", 7, 15), ("Evening", 15, 23), ("Night", 23, 31)]


def coverage(eng):
    v = eng.cfg.get("views", {}).get("coverage")
    if not v:
        return None
    ents, recs, issues = _load(eng)
    blocked = defaultdict(list)   # people who should not count toward coverage
    not_blocking = set(v.get("not_blocking", []))
    for i in issues:
        if i["rule"] == "conflict" and i["attribute"] in not_blocking:
            continue
        if i["severity"] == "critical" and i["category"] in ("Credentials", "Identity"):
            blocked[i["entity_key"]].append(i["title"])
    grid = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    dates = set()
    for r in recs:
        if r["source"] != v["source"]:
            continue
        d = r["norm"].get("date")
        if d:
            dates.add(d)
        h, start = r["norm"].get("hours") or 0, r["norm"].get("_start")
        if not h or start is None or not d:
            continue
        ent = ents.get(r["entity_key"])
        role = _val(ent, "role") if ent and ent["status"] == "verified" else r["norm"].get("role")
        s, e = start, start + h
        for name, b0, b1 in BANDS:
            for off in (-24, 0, 24):
                ov = min(e, b1 + off) - max(s, b0 + off)
                if ov >= 4 or (ov > 0 and ov >= h * 0.5):
                    fac = r["norm"].get("facility") or "UNKNOWN"
                    grid[fac][d][name].append({"key": r["entity_key"], "name": ent["display_name"] if ent else r["raw"].get("staff_name"),
                                               "role": role, "shift": r["raw"].get("shift"),
                                               "unverified": (ent or {}).get("status") != "verified",
                                               "blocked": blocked.get(r["entity_key"], [])})
                    break
    need = v.get("required_any", [])      # at least one of these roles, with clean credentials
    out = {}
    for fac, by_date in grid.items():
        cells, ok = [], 0
        for d in sorted(dates):
            for name, _, _ in BANDS:
                staff = by_date.get(d, {}).get(name, [])
                clean = [p for p in staff if not p["blocked"] and not p["unverified"]]
                if any(p["role"] in need for p in clean):
                    status = "ok"
                elif any(p["role"] in need for p in staff):
                    status = "at_risk"          # a nurse is scheduled, but their record has a critical problem
                elif staff:
                    status = "gap"              # people on shift, but no licensed nurse
                else:
                    status = "empty"
                ok += status == "ok"
                cells.append({"date": d, "band": name, "staff": staff, "status": status})
        out[fac] = {"cells": cells, "ready": ok, "total": len(cells)}
    return {"facilities": out, "dates": sorted(dates), "bands": [b[0] for b in BANDS], "required_any": need}
