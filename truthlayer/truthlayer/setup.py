"""Guided setup: look at a client's CSVs and propose a configuration.

analyze(files)            -> profiles of every column + a set of plain-language *choices*
build_config(profiles, choices) -> the same YAML-shaped config a hand-written client file has

The person reviews the choices (which file is the master list, how files link, which
fields to compare, which checks to run) and can change any of them before saving.
Nothing here is industry-specific; it works from column names and, mostly, from values.
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from datetime import date
from .ingest import read_table, is_pdf, looks_like_grid, ingest_weekly_grid, shift_hours, _clean_shift, OFF_CODES, LEAVE_CODES
from .normalize import (simplify, is_empty, norm_date, norm_phone, norm_id, norm_license,
                        parse_name, parse_org, name_similarity, name_key)

NAME_HINT = re.compile(r"\b(name|employee|customer|client|recipient|staff|licensee|holder|patient|member|contact|person|worker|bill to|ship to|tenant|student|driver|guest)\b")
ORG_HINT = re.compile(r"\b(company|vendor|supplier|organization|organisation|business|carrier|manufacturer|partner|firm|account name|employer|merchant)\b")
FIRST_HINT = re.compile(r"\b(first|given|fname|forename)\b")
LAST_HINT = re.compile(r"\b(last|surname|family|lname)\b")
ID_HINT = re.compile(r"(\bid\b|\bno\b|\bnumber\b|\bnum\b|#|\bcode\b|\bkey\b|\bsku\b|\bref\b|\bupc\b|\biban\b|\baccount\b)")
EXP_HINT = re.compile(r"(expir|\bexp\b|valid until|valid to|due|renew|deadline|lapse|end date|until)")
VERIFY_HINT = re.compile(r"(verif|checked|audit|inspect|reviewed|confirmed|last seen|last updated)")
START_HINT = re.compile(r"(start|from|begin)")
END_HINT = re.compile(r"(\bend\b|\bto\b|through|thru|finish)")
MONEY_HINT = re.compile(r"(amount|price|cost|total|value|revenue|billed|invoice|balance|fee|pay\b|gross|net|wage|salary|\$)")
MILES_HINT = re.compile(r"(mile|\bmi\b|\bkm\b|distance)")
STATUS_HINT = re.compile(r"\bstatus\b|\bstanding\b")
BAD_STATUS = re.compile(r"(suspend|revok|expire|terminat|inactive|denied|fail|cancel|lapsed|invalid|disqualif|closed|blocked)", re.I)
ACRONYMS = {"hr", "hris", "crm", "erp", "eld", "dot", "cdl", "iso", "mvr", "sku", "po", "ap", "ar", "pos", "id", "ehr", "emr", "osha", "faa", "dea", "npi", "ein", "vin", "usa", "uk", "llc"}
HOURS_HINT = re.compile(r"(hour|hrs)")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
CODEID_RE = re.compile(r"^[A-Za-z]{1,6}[\s\-#_.]?\d{3,}$")
NUM_RE = re.compile(r"^-?\$?\s?[\d,]*\.?\d+$")
STOP = {"id", "no", "number", "num", "code", "key", "the", "of", "date", "name"}


def snake(h: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "", simplify(h).replace(" ", "_"))
    return (s or "col")[:48]


def slug(fn: str) -> str:
    base = re.sub(r"\.[A-Za-z0-9]+$", "", fn.lower())
    s = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return (s or "file")[:32]


def nice_label(sid: str) -> str:
    small = {"and", "the", "for", "of", "to", "in", "on", "by", "new", "all", "old", "med", "pay", "log", "day", "car", "van"}
    return " ".join(w.upper() if w in ACRONYMS or (len(w) <= 3 and w not in small and not w.isdigit()) else w.capitalize() for w in sid.split("_"))


def smart_cap(text: str) -> str:
    words = text.split()
    out = [w.upper() if w.lower() in ACRONYMS else w for w in words]
    if out and out[0] == words[0]:
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


def human(attr: str) -> str:
    return attr.replace("_", " ")


# ------------------------------------------------------------------ column profiling
def _frac(vals, pred):
    return sum(1 for v in vals if pred(v)) / len(vals) if vals else 0


def profile_column(header: str, vals: list[str]) -> dict:
    vals = [v.strip() for v in vals if not is_empty(v)]
    n = len(vals)
    h = simplify(header)
    raw_h = header.lower()
    id_hint = bool(ID_HINT.search(h) or "#" in raw_h or re.search(r"\b(emp|employee|acct|account)\s*(#|no\.?|num)", raw_h))
    distinct = len(set(vals))
    uniq = distinct / n if n else 0
    avg_len = sum(len(v) for v in vals) / n if n else 0
    t = "text"
    if n == 0:
        t = "text"
    elif _frac(vals, EMAIL_RE.match) >= .8:
        t = "email"
    elif _frac(vals, lambda v: norm_date(v)[0] is not None and not re.fullmatch(r"\d{1,5}", v)) >= .8 and not id_hint:
        t = "date"
    elif _frac(vals, lambda v: len(re.sub(r"\D", "", v)) in (10, 11) and re.fullmatch(r"[\d\s()+.\-]+", v)) >= .8 and ("phone" in h or "mobile" in h or "tel" in h or "cell" in h or "fax" in h):
        t = "phone"
    elif _frac(vals, lambda v: NUM_RE.match(v.replace(" ", ""))) >= .9 and not (id_hint and uniq >= .95):
        t = "number"
    elif FIRST_HINT.search(h) and _frac(vals, lambda v: re.search(r"[A-Za-z]", v)) >= .9:
        t = "first_name"
    elif LAST_HINT.search(h) and _frac(vals, lambda v: re.search(r"[A-Za-z]", v)) >= .9:
        t = "last_name"
    elif _frac(vals, lambda v: CODEID_RE.match(v)) >= .8:
        t = "codeid"
    elif ORG_HINT.search(h) and not ID_HINT.search(h):
        t = "org_name"
    elif (NAME_HINT.search(h) and not ID_HINT.search(h) and _frac(vals, lambda v: re.search(r"[A-Za-z]", v) and (" " in v or "," in v)) >= .7) \
            or _frac(vals, lambda v: re.fullmatch(r"[A-Z][A-Za-z'’\-]+,\s*[A-Z][A-Za-z'’\-. ]+", v)) >= .8:
        t = "person_name"
    elif id_hint and uniq >= .95 and avg_len <= 24:
        t = "id"
    elif n >= 3 and distinct <= max(6, n * .6) and avg_len <= 40:
        t = "code"
    nums = []
    if t == "number":
        for v in vals:
            try:
                nums.append(float(v.replace(",", "").replace("$", "").replace(" ", "")))
            except ValueError:
                pass
    return {
        "header": header, "field": snake(header), "type": t, "n": n, "blank": 0, "distinct": distinct,
        "unique": round(uniq, 3), "sample": list(dict.fromkeys(vals))[:6],
        "values": list(dict.fromkeys(vals))[:600],
        "all_values": vals[:5000] if bool(STATUS_HINT.search(h)) else None,
        "expiry": t == "date" and bool(EXP_HINT.search(h)), "verified": t == "date" and bool(VERIFY_HINT.search(h)),
        "start": t == "date" and bool(START_HINT.search(h)), "end": t == "date" and bool(END_HINT.search(h)),
        "min": min(nums) if nums else None, "max": max(nums) if nums else None,
        "money": t == "number" and bool(MONEY_HINT.search(h)), "hours": t == "number" and bool(HOURS_HINT.search(h)),
        "miles": t == "number" and bool(MILES_HINT.search(h)), "status": bool(STATUS_HINT.search(h)),
    }


def _header_row(rows):
    best, score = 0, -1
    for i, r in enumerate(rows[:10]):
        cells = [c for c in r if c.strip()]
        if len(cells) < 2:
            continue
        s = len(cells) - sum(1 for c in cells if NUM_RE.match(c.replace(" ", ""))) * 2 + (len(set(cells)) == len(cells))
        if s > score:
            best, score = i, s
    return best


GRID_HEADERS = ["Staff Name", "Role", "Facility", "Date", "Shift", "Hours"]


def grid_rows(data: bytes, filename: str) -> list[list[str]]:
    """A printed schedule grid (people x days) -> one row per person per day, with hours worked out."""
    cfg = {"sources": {"_grid": {"fields": {}}}, "reference": {}}
    recs, _ = ingest_weekly_grid(data, filename, cfg, date.today(), "_grid")
    rows = [GRID_HEADERS]
    for r in recs:
        raw = r["raw"]
        c = _clean_shift(raw.get("shift", ""))
        if c in OFF_CODES or c in LEAVE_CODES:
            h = 0
        else:
            h = shift_hours(raw.get("shift", ""), raw.get("_legend") or {})[0] or 0
        rows.append([raw.get("staff_name", ""), raw.get("role", ""), raw.get("facility", ""), raw.get("date", ""), raw.get("shift", ""), str(h)])
    return rows


def profile_file(filename: str, data: bytes) -> dict:
    grid = is_pdf(data, filename) and looks_like_grid(data)
    rows = grid_rows(data, filename) if grid else read_table(data, filename)
    hi = _header_row(rows)
    headers = [h.strip() or f"column {i + 1}" for i, h in enumerate(rows[hi])]
    body = [r for r in rows[hi + 1:] if any(c.strip() for c in r)]
    cols = []
    seen = set()
    for i, h in enumerate(headers):
        p = profile_column(h, [r[i] if i < len(r) else "" for r in body])
        p["blank"] = round(1 - p["n"] / len(body), 3) if body else 0
        f = p["field"]
        while f in seen:
            f += "_2"
        p["field"] = f
        seen.add(f)
        cols.append(p)
    sid = slug(filename)
    full = next((c for c in cols if c["type"] in ("person_name", "org_name")), None)
    first = next((c for c in cols if c["type"] == "first_name"), None)
    last = next((c for c in cols if c["type"] == "last_name"), None)
    names = []
    if first and last:
        for r in body:
            p = parse_name(first=r[headers.index(first["header"])] if headers.index(first["header"]) < len(r) else "",
                           last=r[headers.index(last["header"])] if headers.index(last["header"]) < len(r) else "")
            if p:
                names.append(p)
        name_spec = {"first": first["field"], "last": last["field"]}
    elif full:
        parse = parse_org if full["type"] == "org_name" else (lambda v: parse_name(full=v))
        names = [p for p in (parse(v) for v in full["values"]) if p]
        name_spec = {"full": full["field"]}
    else:
        name_spec = None
    keys = [c for c in cols if c["type"] in ("id", "codeid", "email") and c["unique"] >= .95 and c["n"] >= max(1, .9 * len(body))]
    ent_ratio = 1.0
    if name_spec and body:
        ent_ratio = len({name_key(p) for p in names}) / max(1, len(body)) if names else 1
    elif keys:
        ent_ratio = 1.0
    elif body:
        ent_ratio = 0.5
    # a file with dated amounts/quantities is a log of activity (pay periods, invoices, shipments), not a profile
    has_measure = any(c["type"] == "number" for c in cols) and any(c["type"] == "date" and not (c["expiry"] or c["verified"]) for c in cols)
    kind = "activity" if (ent_ratio < .8 or has_measure) else "profile"
    if grid:
        for c in cols:            # schedule cells like "OFF" make hours look sparse; they are still hours
            if c["field"] == "hours":
                c["hours"] = True
    return {"sid": sid, "filename": filename, "label": nice_label(sid), "rows": len(body), "columns": cols,
            "format": "pdf" if is_pdf(data, filename) else "csv", "grid": grid,
            "name_spec": name_spec, "names": [{"first": n["first"], "last": n["last"], "display": n["display"], **({"org": True} if n.get("org") else {})} for n in names][:600],
            "keys": [c["field"] for c in keys], "kind": kind}


# ------------------------------------------------------------------ value comparisons
def _norm_for(t, v):
    if t == "id":
        return norm_id(v)[0]
    if t == "codeid":
        return norm_license(v)[0]
    if t == "email":
        return str(v).strip().lower()
    if t == "phone":
        return norm_phone(v)[0]
    if t == "date":
        return norm_date(v)[0]
    return simplify(v)


def _initials(s):
    return "".join(w[0] for w in simplify(s).split() if w)


def _subseq(short, long):
    it = iter(long)
    return all(ch in it for ch in short)


def alias_match(u: str, canon: list[str]) -> str | None:
    """Map a coded value to one of the canonical values: 'BYS'→'Harborview Bayside', 'RN'→'Registered Nurse'."""
    su = simplify(u)
    if not su:
        return None
    sc = {c: simplify(c) for c in canon}
    exact = [c for c in canon if sc[c] == su]
    if exact:
        return exact[0]
    cont = [c for c in canon if sc[c] and (re.search(rf"\b{re.escape(su)}\b", sc[c]) or re.search(rf"\b{re.escape(sc[c])}\b", su))]
    if len(cont) == 1:
        return cont[0]
    cu = su.replace(" ", "")
    ini = [c for c in canon if _initials(c) == cu]
    if len(ini) == 1:
        return ini[0]
    if 2 <= len(cu) <= 6:
        sub = [c for c in canon if any(w[:1] == cu[:1] and _subseq(cu, w) for w in sc[c].split())]
        if len(sub) == 1:
            return sub[0]
        sub = [c for c in canon if sc[c][:1] == cu[:1] and _subseq(cu, sc[c].replace(" ", ""))]
        if len(sub) == 1:
            return sub[0]
    # shared word + the remaining words are abbreviations ("HV Riverdale" ~ "Harborview Riverdale")
    def abbrev_ok(c):
        a, b = set(su.split()), set(sc[c].split())
        shared = (a & b) - STOP
        if not shared:
            return False
        rest_a, rest_b = a - b, b - a
        return all(any(w[:1] == x[:1] and _subseq(w, x) for x in rest_b) for w in rest_a)
    tok = [c for c in canon if abbrev_ok(c)]
    if len(tok) == 1:
        return tok[0]
    fz = sorted(((SequenceMatcher(None, su, sc[c]).ratio(), c) for c in canon), reverse=True)
    if fz and fz[0][0] >= .85 and (len(fz) == 1 or fz[1][0] < fz[0][0] - .05):
        return fz[0][1]
    return None


def cluster_values(primary: list[str], others: list[str] = ()) -> dict:
    """Group spellings of the same value. Longest spellings become the canonical names."""
    canon, mapping = [], {}
    for v in sorted(dict.fromkeys(primary), key=lambda x: -len(x)):
        m = alias_match(v, canon) if canon else None
        if m:
            mapping[v] = m
        else:
            canon.append(v)
            mapping[v] = v
    for v in dict.fromkeys(others):
        if v in mapping:
            continue
        m = alias_match(v, canon)
        mapping[v] = m or v
        if not m:
            canon.append(v)
    return mapping


def compatible(a, b):
    fam = lambda t: {"id": "key", "codeid": "key"}.get(t, t)
    return fam(a) == fam(b)


def value_overlap(ca: dict, cb: dict) -> float:
    """Share of cb's distinct values that also appear in ca (after normalizing / aliasing)."""
    if not ca["values"] or not cb["values"]:
        return 0.0
    if ca["type"] == "code" or cb["type"] == "code":
        hit = sum(1 for v in cb["values"] if alias_match(v, ca["values"]))
        return hit / len(cb["values"])
    t = ca["type"] if ca["type"] in ("id", "codeid", "email", "phone", "date") else cb["type"]
    A = {_norm_for(t, v) for v in ca["values"]} - {None}
    B = {_norm_for(t, v) for v in cb["values"]} - {None}
    return len(A & B) / len(B) if B else 0.0


HEADER_SYN = {"title": "role", "position": "role", "job": "role", "occupation": "role", "discipline": "role",
              "site": "facility", "location": "facility", "branch": "facility", "terminal": "facility", "store": "facility",
              "unit": "facility", "department": "dept", "dept": "dept", "mail": "email", "e": "email"}


def header_sim(a, b):
    canon = lambda h: {HEADER_SYN.get(t, t) for t in simplify(h).split()} - STOP
    ta, tb = canon(a), canon(b)
    if ta and tb and (ta & tb):
        return len(ta & tb) / len(ta | tb) + .3
    return SequenceMatcher(None, simplify(a), simplify(b)).ratio() * .6


def _as_org(n):
    return n if n.get("org") else parse_org(n["display"])


def name_overlap(pa: dict, pb: dict) -> float:
    if not pa["names"] or not pb["names"]:
        return 0.0
    hits = 0
    A, B = pa["names"], pb["names"]
    if any(n.get("org") for n in A) or any(n.get("org") for n in B):   # companies: compare as organisations
        A, B = [_as_org(n) for n in A], [_as_org(n) for n in B]
    distinct = {name_key(n): n for n in B}
    for n in distinct.values():
        if any(name_similarity(n, m)[0] >= .85 for m in A):
            hits += 1
    return hits / len(distinct)


# ------------------------------------------------------------------ the proposal
ENTITY_WORDS = {"employee": "Employee", "staff": "Employee", "hr": "Employee", "roster": "Employee", "customer": "Customer",
                "crm": "Customer", "client": "Client", "vendor": "Supplier", "supplier": "Supplier", "patient": "Patient",
                "member": "Member", "student": "Student", "product": "Product", "item": "Product", "sku": "Product",
                "driver": "Driver", "tenant": "Tenant", "account": "Account", "property": "Property", "asset": "Asset", "contractor": "Contractor"}


def guess_entity(master: dict) -> str:
    texts = [simplify(master["filename"])] + [simplify(c["header"]) for c in master["columns"] if c["field"] in master["keys"]]
    for t in texts:
        for w in t.split():
            if w in ENTITY_WORDS:
                return ENTITY_WORDS[w]
            if w.endswith("s") and w[:-1] in ENTITY_WORDS:
                return ENTITY_WORDS[w[:-1]]
    return "Record"


def analyze(files: list[tuple[str, bytes]], master_sid: str | None = None) -> dict:
    profiles, skipped = [], []
    for fn, data in files:
        try:
            p = profile_file(fn, data)
        except Exception as e:  # noqa
            skipped.append({"filename": fn, "reason": "We couldn't find a table in this PDF (it may be a scanned image)." if is_pdf(data, fn) else f"Couldn't read it: {e}"})
            continue
        base, k = p["sid"], 2
        while any(x["sid"] == p["sid"] for x in profiles):
            p["sid"] = f"{base}_{k}"
            k += 1
        profiles.append(p)
    if not profiles:
        return {"profiles": [], "choices": None, "skipped": skipped}

    # how strongly every pair of files is connected (keys first, then names)
    def links_between(master, other):
        out = []
        for oc in other["columns"]:
            if oc["type"] not in ("id", "codeid", "email", "phone"):
                continue
            for mc in master["columns"]:
                if mc["type"] in ("id", "codeid", "email", "phone") and compatible(mc["type"], oc["type"]):
                    ov = value_overlap(mc, oc)
                    if ov >= .5:
                        out.append({"type": "key", "column": oc["field"], "master_column": mc["field"], "score": round(ov, 2), "use": True})
        out.sort(key=lambda l: -l["score"])
        # one link per column on each side
        used_o, used_m, keep = set(), set(), []
        for l in out:
            if l["column"] in used_o or l["master_column"] in used_m:
                continue
            used_o.add(l["column"]); used_m.add(l["master_column"]); keep.append(l)
        if other["name_spec"] and master["name_spec"]:
            no = name_overlap(master, other)
            if no >= .4:
                col = other["name_spec"].get("full") or other["name_spec"].get("last")
                keep.append({"type": "name", "column": col, "score": round(no, 2), "use": True})
        return keep

    def master_score(p):
        s = 0
        s += 3 if p["keys"] else 0
        s += 2 if p["name_spec"] else 0
        s += 2 if p["kind"] == "profile" else 0
        s += sum(1 for o in profiles if o is not p and links_between(p, o))
        s += 2 if re.search(r"(roster|master|crm|hr|employees|customers|clients|directory|vendors|suppliers|members|products|catalog)", p["sid"]) else 0
        s += len(p["columns"]) / 100
        return s

    master = next((p for p in profiles if p["sid"] == master_sid), None) or max(profiles, key=master_score)
    P_ = {p["sid"]: p for p in profiles}
    key = next((c["field"] for c in master["columns"] if c["field"] in master["keys"] and c["type"] == "id"), None) \
        or (master["keys"][0] if master["keys"] else None)
    if not key:   # no ID column at all: use the first fully-unique column, else the name
        key = next((c["field"] for c in master["columns"] if c["unique"] == 1 and c["n"] == master["rows"] and c["type"] != "number"), None) \
            or (master["name_spec"] or {}).get("full") or master["columns"][0]["field"]
    choices = {"entity_label": guess_entity(master), "master": master["sid"], "key": key, "sources": {}, "attributes": [], "rules": []}
    for p in profiles:
        ls = [] if p is master else links_between(master, p)
        choices["sources"][p["sid"]] = {"label": p["label"], "include": True, "kind": "master" if p is master else p["kind"],
                                        "links": ls, "rows": p["rows"], "filename": p["filename"]}

    # ---- attributes: master columns, plus matching columns from the other files
    mcols = {c["field"]: c for c in master["columns"]}
    name_cols = set((master["name_spec"] or {}).values())
    attrs = []
    if master["name_spec"]:
        a = {"name": "name", "type": "person_name", "cols": {master["sid"]: "_name"}, "authority": master["sid"], "include": True}
        if any(n.get("org") for n in master["names"]):
            a["type"] = "org_name"
        attrs.append(a)
    for c in master["columns"]:
        if c["field"] == key or c["field"] in name_cols or c["type"] in ("number",):
            continue
        attrs.append({"name": c["field"], "type": c["type"], "cols": {master["sid"]: c["field"]}, "authority": master["sid"],
                      "include": True, "expiry": c["expiry"], "verified": c["verified"]})
    for p in profiles:
        if p is master or not choices["sources"][p["sid"]]["links"]:
            continue
        if p["name_spec"] and attrs and attrs[0]["name"] == "name":
            attrs[0]["cols"][p["sid"]] = p["name_spec"].get("full") or "_name"
        pname = set((p["name_spec"] or {}).values())
        for oc in p["columns"]:
            if oc["field"] in pname or oc["type"] == "number":
                continue
            best, bs = None, 0
            for a in attrs:
                if a["name"] == "name" or p["sid"] in a["cols"]:
                    continue
                mc = mcols.get(a["cols"].get(master["sid"]))
                if not mc or not (compatible(mc["type"], oc["type"]) or {mc["type"], oc["type"]} <= {"code", "text"}):
                    continue
                ov = value_overlap(mc, oc)
                hs = header_sim(mc["header"], oc["header"])
                score = ov + .4 * min(1, hs)
                if oc["type"] == "date" and not (hs >= .5 or ov >= .7):   # dates coincide by accident; need a name hint too
                    continue
                need_hint = p["kind"] == "activity" or ov < .7
                if need_hint and hs < .5:
                    continue
                if (ov >= .4 or (hs >= .9 and ov >= .1)) and score > bs:
                    best, bs = a, score
            if best:
                best["cols"][p["sid"]] = oc["field"]
                best["expiry"] = best.get("expiry") or oc["expiry"]
                best["verified"] = best.get("verified") or oc["verified"]
                if oc["type"] == "code" or mcols[best["cols"][master["sid"]]]["type"] == "code":
                    best["type"] = "code"
            elif p["kind"] == "profile" and oc["type"] in ("date", "code", "email", "phone", "codeid", "id", "text") \
                    and not any(l["column"] == oc["field"] for l in choices["sources"][p["sid"]]["links"]):
                nm = oc["field"] if oc["field"] not in {a["name"] for a in attrs} else f"{p['sid']}_{oc['field']}"
                attrs.append({"name": nm, "type": oc["type"], "cols": {p["sid"]: oc["field"]}, "authority": p["sid"], "include": True,
                              "expiry": oc["expiry"], "verified": oc["verified"]})
    # code columns compared across files become coded vocabularies; text stays text
    for a in attrs:
        if a["type"] == "code" and len(a["cols"]) < 2:
            a["type"] = "text"
    # who to trust: the master, except dates of expiry/verification which come from the dedicated file if one exists
    for a in attrs:
        if (a.get("expiry") or a.get("verified")) and len(a["cols"]) > 1:
            other = next((s for s in a["cols"] if s != master["sid"] and choices["sources"][s]["kind"] == "profile"), None)
            if other:
                a["authority"] = other
    choices["attributes"] = attrs

    # ---- checks
    rules = []
    for a in attrs:
        if a.get("expiry"):
            label = smart_cap(re.sub(r"\b(expiration|expiry|expires|exp|date|valid|until|due|end)\b", "", human(a["name"])).strip())
            if not label:   # e.g. "Valid Until" in iso_certifications.csv -> "ISO certification"
                src = choices["sources"][a["authority"]]["label"]
                label = re.sub(r"s$", "", src.split()[-1]) if src else "Item"
                label = (" ".join(src.split()[:-1] + [label])).strip()
                label = label[:1].upper() + label[1:]
            rules.append({"type": "expiration", "id": f"{a['name']}_expiry", "attribute": a["name"], "label": label,
                          "warn_days": 60, "critical_days": 0, "include": True, "category": "Expiring",
                          "explain": f"Flag when {human(a['name'])} has passed, or is within {{warn_days}} days."})
            acts = {}
            for p in profiles:
                if p["kind"] == "activity" and choices["sources"][p["sid"]]["links"]:
                    dc = [c for c in p["columns"] if c["type"] == "date"]
                    d = next((c for c in dc if c["end"]), dc[0] if dc else None)
                    if d:
                        acts[p["sid"]] = {"date": d["field"], "label": f"{p['label']} record"}
            if acts:
                rules.append({"type": "activity_after_expiry", "id": f"{a['name']}_activity_after", "attribute": a["name"],
                              "activity": acts, "severity": "critical", "category": "Expiring", "title": f"Active after {label if label[:2].isupper() else label[:1].lower() + label[1:]} expired", "include": True,
                              "explain": f"Flag records in {', '.join(choices['sources'][s]['label'] for s in acts)} dated after the {human(a['name'])}."})
        if a.get("verified"):
            rules.append({"type": "staleness", "id": f"{a['name']}_stale", "attribute": a["name"], "label": smart_cap(human(a["name"])),
                          "max_age_days": 180, "include": True, "category": "Expiring", "explain": f"Flag when {human(a['name'])} is more than {{max_age_days}} days old."})
    for p in profiles:
        if p is master or not choices["sources"][p["sid"]]["links"]:
            continue
        rules.append({"type": "presence", "id": f"missing_from_{p['sid']}", "include": p["kind"] == "profile",
                      "expect": {p["sid"]: {"severity": "warning" if p["kind"] == "profile" else "info", "category": "Completeness",
                                            "message": f"Not found in {p['label']}"}},
                      "explain": f"Flag {choices['entity_label'].lower()}s who have no record in {p['label']}."})
    dup_on = [a["name"] for a in attrs if master["sid"] in a["cols"] and a["type"] in ("email", "phone", "codeid")
              and mcols.get(a["cols"][master["sid"]], {}).get("unique", 0) >= .8]
    if dup_on:
        rules.append({"type": "duplicate_entity", "id": "possible_duplicates", "match_on": dup_on, "severity": "warning", "include": True,
                      "explain": f"Flag two {choices['entity_label'].lower()}s that share the same {', '.join(human(x) for x in dup_on)}."})
    acts = [p for p in profiles if p["kind"] == "activity" and choices["sources"][p["sid"]]["links"]]
    for A in acts:
        dA = [c for c in A["columns"] if c["type"] == "date"]
        s_, e_ = next((c for c in dA if c["start"]), None), next((c for c in dA if c["end"]), None)
        mA = [c for c in A["columns"] if c["type"] == "number" and c["field"] not in A["keys"]]
        if not (s_ and e_ and mA):
            continue
        rules.append({"type": "date_order", "id": f"{A['sid']}_period_order", "source": A["sid"], "start": s_["field"], "end": e_["field"],
                      "severity": "warning", "include": True, "explain": f"Flag {A['label']} rows whose end date is before the start date."})
        for B in acts:
            if B is A:
                continue
            dB = [c for c in B["columns"] if c["type"] == "date"]
            mB = [c for c in B["columns"] if c["type"] == "number" and c["field"] not in B["keys"]]
            if not (dB and mB):
                continue
            kind = lambda c: "money" if c["money"] else "hours" if c["hours"] else "miles" if c["miles"] else ""
            ma, mb = max(((x, y) for x in mA for y in mB),
                         key=lambda xy: (kind(xy[0]) == kind(xy[1]) and kind(xy[0]) != "", header_sim(xy[0]["header"], xy[1]["header"]),
                                         abs(xy[0]["max"] or 0)))
            if not (kind(ma) == kind(mb) and kind(ma)) and header_sim(ma["header"], mb["header"]) < .5:
                continue      # nothing that measures the same thing
            money = kind(ma) == "money"
            rules.append({"type": "aggregate_compare", "id": f"{A['sid']}_vs_{B['sid']}", "label": f"{smart_cap(human(ma['field']))} ({A['label']}) vs {human(mb['field'])} ({B['label']})",
                          "a": {"source": A["sid"], "measure": ma["field"], "start": s_["field"], "end": e_["field"]},
                          "b": {"source": B["sid"], "measure": mb["field"], "date": dB[0]["field"]},
                          **({"coverage": "every_day"} if B.get("grid") else {"coverage": "date_range", "slack_days": 14}), "tolerance": 1 if money else .5, "severity": "warning", "category": "Totals",
                          **({"prefix": "$"} if money else {"unit": " h"} if kind(ma) == "hours" else {"unit": " mi"} if kind(ma) == "miles" else {}),
                          "action": "Confirm which total is right.", "include": True,
                          "explain": f"For each {choices['entity_label'].lower()} and period, compare total {human(ma['field'])} in {A['label']} with total {human(mb['field'])} in {B['label']}."})
    # status columns: flag anything that isn't the normal status ("SUSPENDED" when most are "VALID")
    for a in attrs:
        sid = a["authority"] if a.get("authority") in a["cols"] else next(iter(a["cols"]))
        col = next((c for c in P_[sid]["columns"] if c["field"] == a["cols"][sid]), None)
        if not col or not col["status"] or col["n"] < 3:
            continue
        counts = Counter(simplify(v) for v in col.get("all_values", col["values"]))
        normal = [v for v, n in counts.items() if n / col["n"] >= .5 and not BAD_STATUS.search(v)]
        if not normal or len(counts) < 2:
            continue
        rules.append({"type": "unusual_value", "id": f"{a['name']}_unusual", "attribute": a["name"], "expected": normal,
                      "label": smart_cap(human(a["name"])), "severity": "warning", "critical_pattern": BAD_STATUS.pattern, "include": True,
                      "category": "Status", "explain": f"Flag when {human(a['name'])} is anything other than {', '.join(normal)}."})
    choices["rules"] = rules
    return {"profiles": profiles, "choices": choices, "skipped": skipped}


# ------------------------------------------------------------------ choices -> config
FIELD_TYPE = {"id": "id", "codeid": "license", "email": "email", "phone": "phone", "date": "date", "number": "number",
              "person_name": "person_name", "org_name": "org_name", "first_name": "text", "last_name": "text", "code": "text", "text": "text"}


def build_config(client: str, profiles: list[dict], choices: dict) -> dict:
    P = {p["sid"]: p for p in profiles}
    included = [sid for sid, s in choices["sources"].items() if s.get("include", True) and sid in P]
    master = choices["master"]
    attrs = [a for a in choices["attributes"] if a.get("include", True)]
    # vocabularies for coded attributes compared across files
    reference, field_ref = {}, {}
    for a in attrs:
        if a["type"] != "code":
            continue
        cols = [(sid, c) for sid, c in a["cols"].items() if sid in included]
        if len(cols) < 2:
            continue
        canon_sid = a.get("authority") if a.get("authority") in a["cols"] else cols[0][0]
        colvals = {sid: next(c for c in P[sid]["columns"] if c["field"] == f)["values"] for sid, f in cols}
        mapping = cluster_values(colvals[canon_sid], [v for sid, vs in colvals.items() if sid != canon_sid for v in vs])
        groups = {}
        for v, c in mapping.items():
            groups.setdefault(c, set())
            if simplify(v) != simplify(c):
                groups[c].add(v)
        reference[a["name"]] = {c: sorted(vs) for c, vs in groups.items()}
        for sid, f in cols:
            field_ref[(sid, f)] = a["name"]

    key_types = {}
    for sid in included:
        for l in choices["sources"][sid].get("links", []):
            if l["type"] == "key" and l.get("use", True):
                mc = next(c for c in P[master]["columns"] if c["field"] == l["master_column"])
                key_types[(sid, l["column"])] = "id" if l["master_column"] == choices.get("key") else mc["type"]

    sources = {}
    for sid in included:
        p, sc = P[sid], choices["sources"][sid]
        fields = {}
        for c in p["columns"]:
            t = key_types.get((sid, c["field"]), c["type"])
            spec = {"type": FIELD_TYPE.get(t, "text"), "synonyms": sorted({c["header"]} - {c["field"]})}
            if (sid, c["field"]) in field_ref:
                spec = {"type": "code", "ref": field_ref[(sid, c["field"])], "synonyms": spec["synonyms"]}
            if t == "number" and c["min"] is not None and c["min"] >= 0:
                spec["min"] = 0
            linked = (sid, c["field"]) in key_types or (sid == master and any(
                l["type"] == "key" and l.get("use", True) and l["master_column"] == c["field"]
                for x in included for l in choices["sources"][x].get("links", [])))
            if c["field"] == (p["keys"][0] if p["keys"] else None) and spec["type"] == "license" and not linked:
                spec["type"] = "id"    # a row's own record number: keep it exactly as written
            if (sid == master and c["field"] == choices.get("key")):
                spec["required"] = True
                if spec["type"] == "license":
                    spec["type"] = "id"
            if p["name_spec"] and c["field"] in p["name_spec"].values():
                spec["required"] = True
            if not spec["synonyms"]:
                del spec["synonyms"]
            fields[c["field"]] = spec
        master_org = any(n.get("org") for n in P[master]["names"])
        if master_org and p["name_spec"] and p["name_spec"].get("full") in fields:
            fields[p["name_spec"]["full"]]["type"] = "org_name"
        src = {"label": sc.get("label") or p["label"], "format": p.get("format", "csv"), "fields": fields}
        if p.get("grid"):
            src["parser"] = "weekly_grid"
        if p["name_spec"]:
            src["name"] = p["name_spec"]
        rk = next((k for k in p["keys"]), None)
        if rk:
            src["record_key"] = rk
        if sid != master:
            links = [l for l in sc.get("links", []) if l.get("use", True)]
            keys = []
            for l in links:
                if l["type"] == "key":
                    ea = "__key__" if l["master_column"] == choices.get("key") else next(
                        (a["name"] for a in attrs if a["cols"].get(master) == l["master_column"]), None)
                    if ea:
                        keys.append({"field": l["column"], "entity_attribute": ea})
            m = {"keys": keys} if keys else {}
            if any(l["type"] == "name" for l in links):
                if keys:
                    m["fallback"] = "name"
                else:
                    m["by"] = "name"
            if not m:
                m = {"by": "none"}
            hints = {a["name"]: a["cols"][sid] for a in attrs if a["type"] == "code" and sid in a["cols"] and master in a["cols"]}
            if hints and (m.get("by") == "name" or m.get("fallback")):
                m["hints"] = hints
            src["match"] = m
            if p["kind"] == "activity":
                dup = [c["field"] for c in p["columns"] if c["field"] not in p["keys"]]
                if dup:
                    src["duplicate_check"] = dup
                    src["duplicate_category"] = "Duplicates"
        sources[sid] = src

    attributes = {}
    for a in attrs:
        frm = {sid: c for sid, c in a["cols"].items() if sid in included}
        if not frm:
            continue
        spec = {"from": frm, "authority": [a["authority"]] + [s for s in frm if s != a["authority"]] if a.get("authority") in frm else list(frm)}
        if a["name"] == "name":
            spec["compare"] = "person_name"
            spec["category"] = "Identity"
        elif a.get("expiry") or a["type"] in ("codeid", "id"):
            spec["severity"] = "critical" if a.get("expiry") else "warning"
            spec["category"] = "Expiring" if a.get("expiry") else "Mismatches"
        else:
            spec["severity"] = "warning"
            spec["category"] = "Mismatches"
        attributes[a["name"]] = spec

    rules = []
    for r in choices["rules"]:
        if not r.get("include", True):
            continue
        r = {k: v for k, v in r.items() if k not in ("include", "explain")}
        srcs = set()
        if r["type"] == "aggregate_compare":
            srcs = {r["a"]["source"], r["b"]["source"]}
        if r["type"] == "presence":
            srcs = set(r["expect"])
        if r["type"] == "date_order":
            srcs = {r["source"]}
        if r["type"] == "activity_after_expiry":
            r["activity"] = {s: v for s, v in r["activity"].items() if s in included}
        if any(s not in included for s in srcs):
            continue
        if "attribute" in r and r["attribute"] not in attributes:
            continue
        if r["type"] == "duplicate_entity":
            r["match_on"] = [x for x in r["match_on"] if x in attributes]
            if not r["match_on"]:
                continue
        rules.append(r)

    cols = [a["name"] for a in attrs if a["name"] != "name" and a["name"] in attributes][:3]
    expiry = [a["name"] for a in attrs if a.get("expiry") and a["name"] in attributes]
    if expiry:
        cols = [c for c in cols if c != expiry[0]][:2] + [expiry[0]]
    return {
        "client": client,
        "entity": {"type": snake(choices["entity_label"]) or "record", "label": choices["entity_label"], "anchor_source": master,
                   "key_field": choices["key"], "columns": cols,
                   "orphan_severity": {s: "warning" for s in included if s != master}},
        "reference": reference,
        "sources": sources,
        "attributes": attributes,
        "matching": {"auto_accept": 0.92, "review_floor": 0.75},
        "rules": rules,
    }
