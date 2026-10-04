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
            reasons.append(f"{(ent or {}).get('display_name') or 'Unknown'}: not matched to anyone in the system of record yet")
        who = (ent or {}).get("display_name") or r["raw"].get("employee_name") or "?"
        reasons += [f"{who}: {t}" for t in by_record.get(r["id"], []) + by_entity.get(r["entity_key"], [])]
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


# ------------------------------------------------------------------ generic views (any industry)
def expiring(eng):
    """Everything with an expiry date, from the client's `expiration` rules."""
    rules = [r for r in eng.cfg.get("rules", []) if r["type"] == "expiration"]
    if not rules:
        return None
    ents, recs, issues = _load(eng)
    today = eng.as_of()
    out = []
    for r in rules:
        rows = []
        for k, e in ents.items():
            a = e["attrs"].get(r["attribute"], {})
            d = _d(a.get("value"))
            if not d:
                continue
            days = (d - today).days
            status = "expired" if days < r.get("critical_days", 0) else "soon" if days <= r.get("warn_days", 60) else "ok"
            rows.append({"entity_key": k, "name": e["display_name"], "date": a["value"], "days_left": days, "status": status,
                         "disputed": a.get("status") == "CONFLICT", "sources": a.get("sources")})
        rows.sort(key=lambda x: x["days_left"])
        out.append({"label": r.get("label", r["attribute"]), "attribute": r["attribute"], "warn_days": r.get("warn_days", 60), "rows": rows,
                    "counts": {s: sum(1 for x in rows if x["status"] == s) for s in ("expired", "soon", "ok")}})
    return {"groups": out, "as_of": today.isoformat()}


def totals(eng):
    """Per-period totals from two systems that should agree (aggregate_compare rules)."""
    rules = [r for r in eng.cfg.get("rules", []) if r["type"] == "aggregate_compare"]
    if not rules:
        return None
    ents, recs, issues = _load(eng)
    out = []
    for r in rules:
        A, B = r["a"], r["b"]
        flagged = {i["entity_key"] for i in issues if i["rule"] == r["id"]}
        per = defaultdict(lambda: {"a": 0.0, "b": 0.0})
        periods = set()
        for x in recs:
            if x["source"] == A["source"]:
                s, t = x["norm"].get(A["start"]), x["norm"].get(A["end"])
                if s and t:
                    per[(x["entity_key"], s, t)]["a"] += x["norm"].get(A["measure"]) or 0
                    periods.add((s, t))
        for x in recs:
            if x["source"] == B["source"]:
                d = x["norm"].get(B["date"])
                for (s, t) in periods:
                    if d and s <= d <= t:
                        per[(x["entity_key"], s, t)]["b"] += x["norm"].get(B["measure"]) or 0
        # only periods the second system actually covers (same rule the check uses)
        b_dates = {x["norm"].get(B["date"]) for x in recs if x["source"] == B["source"] and x["norm"].get(B["date"])}
        def covered(s, t):
            sd, td = _d(s), _d(t)
            if not b_dates or not sd or not td:
                return False
            if r.get("coverage", "every_day") == "every_day":
                return all((sd + timedelta(i)).isoformat() in b_dates for i in range((td - sd).days + 1))
            slack = timedelta(days=r.get("slack_days", 0))
            return _d(min(b_dates)) - slack <= sd and td <= _d(max(b_dates)) + slack
        ok_periods = {pp for pp in periods if covered(*pp)}
        rows = []
        for (k, s, t), v in sorted(per.items(), key=lambda kv: (kv[0][1], -(abs(kv[1]["a"] - kv[1]["b"])))):
            if (s, t) not in ok_periods or k is None:
                continue
            e = ents.get(k) or {}
            rows.append({"entity_key": k, "name": e.get("display_name", k), "period_start": s, "period_end": t,
                         "a": round(v["a"], 2), "b": round(v["b"], 2), "diff": round(v["a"] - v["b"], 2),
                         "ok": abs(v["a"] - v["b"]) <= r.get("tolerance", 0), "flagged": k in flagged})
        out.append({"id": r["id"], "label": r.get("label", r["id"]), "a_label": A.get("verb", eng.label(A["source"])),
                    "b_label": B.get("verb", eng.label(B["source"])), "prefix": r.get("prefix", ""), "unit": r.get("unit", ""),
                    "rows": rows, "mismatched": sum(1 for x in rows if not x["ok"]), "total": len(rows)})
    return {"groups": out}
