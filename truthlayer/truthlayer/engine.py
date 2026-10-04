"""The reconciliation engine. Generic: everything client-specific comes from the YAML config.

process() is a pure rebuild: raw records -> normalize -> resolve entities -> reconcile
attributes -> run rules -> persist. Human decisions are stored separately and keyed by a
fingerprint of the issue (including the conflicting values), so they are re-applied on
every rebuild and automatically re-open if the underlying data changes.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date

from .ingest import ingest_file, shift_hours, OFF_CODES, LEAVE_CODES, _clean_shift
from .normalize import build_ref, normalize_value, parse_name, name_similarity, name_key, is_empty, simplify
from .store import Store, dumps, now
from . import rules as rule_lib

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}


ACRONYMS = {"cdl", "dot", "hr", "hris", "eld", "id", "iso", "npi", "dea", "vin", "sku", "po", "crm", "erp", "rn", "lpn", "cna", "ein", "mvr"}
WORD_FIX = {"exp": "expiration", "dob": "date of birth", "num": "number", "no": "number", "qty": "quantity", "amt": "amount"}


def words_for(attr: str) -> str:
    """license_exp_date -> 'license expiration date', cdl_class -> 'CDL class'."""
    out = []
    for w in attr.split("_"):
        w = WORD_FIX.get(w, w)
        out.append(w.upper() if w in ACRONYMS else w)
    return " ".join(out)


def pretty(v):
    s = display(v)
    return s.title() if s.isupper() and len(s) >= 4 and s.isalpha() else s


def display(v):
    if isinstance(v, dict) and "display" in v:
        return v["display"]
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return "" if v is None else str(v)


class Engine:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        self.refs = {k: build_ref(v) for k, v in cfg["reference"].items()}
        self.anchor = cfg["entity"]["anchor_source"]
        self.auto = cfg.get("matching", {}).get("auto_accept", 0.92)
        self.floor = cfg.get("matching", {}).get("review_floor", 0.75)

    # ------------------------------------------------------------ settings
    def as_of(self) -> date:
        return date.fromisoformat(self.store.setting("as_of") or date.today().isoformat())

    def label(self, source):
        return self.cfg["sources"].get(source, {}).get("label", source)

    # ------------------------------------------------------------ ingestion
    def ingest(self, data: bytes, filename: str, forced: str | None = None, replace: bool = True):
        sha = hashlib.sha256(data).hexdigest()
        source, rows, notes = ingest_file(data, filename, self.cfg, self.as_of(), forced)
        if replace:
            self.store.x("UPDATE files SET active=0 WHERE source=?", (source,))
        fid = self.store.x("INSERT INTO files(source, filename, sha256, uploaded_at, active, row_count, notes_json) VALUES (?,?,?,?,1,?,?)",
                           (source, filename, sha, now(), len(rows), dumps(notes)))
        self.store.many("INSERT INTO records(file_id, source, row_num, locator, raw_json) VALUES (?,?,?,?,?)",
                        [(fid, source, r["row_num"], r["locator"], dumps(r["raw"])) for r in rows])
        msg = f"Ingested {filename} as {self.label(source)}: {len(rows)} records"
        if notes.get("unmapped_columns"):
            msg += f"; unmapped columns kept as raw: {', '.join(notes['unmapped_columns'])}"
        if notes.get("missing_fields"):
            msg += f"; fields not found: {', '.join(notes['missing_fields'])}"
        self.store.event("ingest", msg)
        return {"file_id": fid, "source": source, "label": self.label(source), "records": len(rows), "notes": notes}

    # ------------------------------------------------------------ normalization
    def normalize_record(self, source: str, raw: dict):
        scfg = self.cfg["sources"][source]
        norm, problems = {}, []
        for f, spec in scfg["fields"].items():
            rv = raw.get(f)
            if source_is_grid(scfg) and f == "hours":
                continue
            v, p = normalize_value(rv, spec, self.refs)
            norm[f] = v
            if spec.get("required") and is_empty(rv) and f in raw:
                problems.append((f, "warning", f"Required field '{f}' is blank"))
            elif spec.get("required") and f not in raw:
                problems.append((f, "warning", f"Required field '{f}' is missing from the file"))
            elif p:
                sev = "info" if spec.get("type") == "phone" else "warning"
                problems.append((f, sev, p))
        if source_is_grid(scfg):
            code = (raw.get("shift") or "").strip()
            c = _clean_shift(code)
            if c in OFF_CODES:
                norm["hours"], norm["_start"] = 0, None
            elif c in LEAVE_CODES:
                norm["hours"], norm["_start"], norm["_leave"] = 0, None, code
            else:
                h, start, p = shift_hours(code, raw.get("_legend") or {})
                norm["hours"], norm["_start"] = h or 0, start
                if p:
                    problems.append(("shift", "info" if h else "warning", p))
        nm = scfg.get("name", {})
        if "first" in nm:
            norm["_name"] = parse_name(first=raw.get(nm["first"]), last=raw.get(nm["last"]))
        elif "full" in nm:
            norm["_name"] = norm.get(nm["full"])
        return norm, problems

    # ------------------------------------------------------------ main pipeline
    def process(self):
        cfg, store = self.cfg, self.store
        self.issues: dict[str, dict] = {}
        decisions = {d["fingerprint"]: d for d in store.q("SELECT * FROM decisions WHERE active=1 ORDER BY id")}
        overrides = {(o["source"], o["name_key"]): o["entity_key"] for o in store.q("SELECT * FROM match_overrides")}
        self.decisions = decisions
        self.auto_rules = [{**r, "cond": json.loads(r["condition_json"] or "null")}
                           for r in store.q("SELECT * FROM auto_rules WHERE active=1 ORDER BY id")]

        rows = store.q("""SELECT r.id, r.source, r.locator, r.raw_json, f.filename FROM records r
                          JOIN files f ON f.id=r.file_id WHERE f.active=1 ORDER BY r.source, r.id""")
        recs = []
        for r in rows:
            if r["source"] not in cfg["sources"]:
                continue
            raw = json.loads(r["raw_json"])
            norm, problems = self.normalize_record(r["source"], raw)
            rk = cfg["sources"][r["source"]].get("record_key")
            recs.append({"id": r["id"], "source": r["source"], "file": r["filename"], "locator": r["locator"],
                         "raw": raw, "norm": norm, "problems": problems, "key": norm.get(rk) if rk else None,
                         "entity": None, "method": None, "score": None, "mstatus": None})
        self.recs = recs
        by_source = defaultdict(list)
        for r in recs:
            by_source[r["source"]].append(r)
        self.by_source = by_source

        # 1. anchor entities ------------------------------------------------
        ents: dict[str, dict] = {}
        key_field = cfg["entity"]["key_field"]
        for r in by_source.get(self.anchor, []):
            k = r["norm"].get(key_field)
            if not k:
                self.add_issue("missing_key", "critical", "Data quality", None, None,
                               f"{self.label(self.anchor)} row has no {key_field}",
                               f"{r['locator']} in {r['file']} cannot become a trusted record without an ID.",
                               "Add the ID in the source system.", [self.ev(r)], fp=("missing_key", r["id"]))
                continue
            if k in ents:
                self.add_issue("duplicate_key", "critical", "Identity", k, None,
                               f"{key_field} {k} appears more than once in {self.label(self.anchor)}",
                               "Two rows share one ID; their values are kept side by side below.",
                               "Remove or correct the duplicate row in the source system.",
                               [self.ev(x) for x in ents[k]["records"][self.anchor]] + [self.ev(r)], fp=("dupkey", k))
            else:
                ents[k] = {"key": k, "status": "verified", "name": r["norm"].get("_name"), "records": defaultdict(list)}
            r["entity"], r["method"], r["score"], r["mstatus"] = k, "system of record", 1.0, "anchor"
            ents[k]["records"][self.anchor].append(r)
        self.ents = ents

        # 2. link other sources --------------------------------------------
        for source, scfg in cfg["sources"].items():
            if source == self.anchor or "match" not in scfg:
                continue
            m = scfg["match"]
            # keys to try, in order (several identifiers are allowed: license #, email, account #...)
            keys = list(m.get("keys") or [])
            if m.get("by") == "key" and m.get("field"):
                keys.insert(0, {"field": m["field"], "entity_attribute": m["entity_attribute"]})
            use_name = m.get("by") == "name" or m.get("fallback") == "name" or (not keys and m.get("by") != "key")
            key_idxs = []
            for kspec in keys:
                ea = kspec["entity_attribute"]
                anchor_field = (cfg["entity"]["key_field"] if ea == "__key__"
                                else cfg["attributes"].get(ea, {}).get("from", {}).get(self.anchor, ea))
                idx = defaultdict(set)
                for e in ents.values():
                    for ar in e["records"][self.anchor]:
                        v = ar["norm"].get(anchor_field)
                        if v:
                            idx[v].add(e["key"])
                key_idxs.append((kspec["field"], idx))
            key_idx = key_idxs or None
            review_groups = {}
            cache = {}
            for r in by_source.get(source, []):
                nm = r["norm"].get("_name")
                hit = None
                for field, idx in key_idxs:
                    v = r["norm"].get(field)
                    if v and v in idx:
                        hit = (field, sorted(idx[v]))
                        break
                if hit:
                    field, cands = hit
                    if len(cands) > 1 and nm:
                        cands.sort(key=lambda k: -name_similarity(nm, ents[k]["name"])[0])
                    self.link(r, cands[0], f"{field.replace('_', ' ')} match", 1.0, "auto")
                    continue
                if not use_name:
                    continue
                if not nm:
                    continue
                nk = name_key(nm)
                hints = tuple((h, r["norm"].get(f)) for h, f in sorted(m.get("hints", {}).items()))
                ck = (nm["first"], nm["last"], hints)
                if ck not in cache:
                    cache[ck] = self.rank(nm, dict(hints))
                ranked = cache[ck]
                top = ranked[0] if ranked else None
                second = ranked[1] if len(ranked) > 1 else None
                status = None
                if top and top[0] >= self.auto:
                    status = "auto"
                    if second and second[0] >= self.auto and top[0] - second[0] < 0.03:
                        status = "review"
                elif top and top[0] >= self.floor:
                    status = "review"
                method = (top[2] if top else "") + (" (via fallback)" if key_idx is not None else "")
                if status == "review":
                    grp = review_groups.setdefault((nk, top[1]), {"recs": [], "top": top, "second": second, "name": nm})
                    grp["recs"].append(r)
                ov = overrides.get((source, nk))
                if ov == "__none__":
                    continue
                if ov and ov in ents:
                    self.link(r, ov, "confirmed by reviewer", 1.0, "confirmed")
                elif status:
                    self.link(r, top[1], method, top[0], status)
            for (nk, ek), g in review_groups.items():
                top, second, nm = g["top"], g["second"], g["name"]
                alt = f" Next best: {ents[second[1]]['name']['display']} ({second[0]:.0%})." if second and second[0] >= self.floor else ""
                self.add_issue("match", "warning", "Identity", ek, None,
                               f"Is '{nm['display']}' in {self.label(source)} the same {self.cfg['entity'].get('label', 'record').lower()}?",
                               f"'{nm['display']}' ({len(g['recs'])} record(s)) looks like {ents[ek]['name']['display']} ({ek}) "
                               f"by {top[2]}, {top[0]:.0%} similar. Linked provisionally; not trusted until confirmed.{alt}",
                               "Confirm if they're the same; reject to treat them as separate.",
                               [self.ev(x) for x in g["recs"]][:6],
                               fp=("match", source, nk, ek), options=["confirm", "reject"],
                               data={"source": source, "name_key": nk, "candidate": ek})

        # 3. cluster leftovers into unverified entities ---------------------
        orphan_sev = cfg["entity"].get("orphan_severity", {})
        clusters = defaultdict(list)
        for r in recs:
            if r["entity"] is not None or r["source"] == self.anchor:
                continue
            if r["norm"].get("_name"):
                clusters[name_key(r["norm"]["_name"])].append(r)
            else:   # no name: group by the first identifier it carries, so it still shows up
                m = cfg["sources"][r["source"]].get("match", {})
                fields = [k["field"] for k in m.get("keys", [])] + ([m["field"]] if m.get("field") else [])
                v = next((r["norm"].get(f) for f in fields if r["norm"].get(f)), None)
                if v:
                    r["norm"]["_name"] = {"first": "", "middle": "", "last": str(v), "display": str(v)}
                    clusters[f"id|{v}"].append(r)
        for nk, rs in clusters.items():
            n0 = rs[0]["norm"]["_name"]
            k = re.sub(r"[^A-Za-z0-9-]+", "-", f"U-{n0['last']}-{n0['first']}" if n0["first"] else f"U-{n0['last']}").strip("-")
            ents[k] = {"key": k, "status": "unverified", "name": rs[0]["norm"]["_name"], "records": defaultdict(list)}
            for r in rs:
                self.link(r, k, "name cluster (no system-of-record match)", None, "unverified")
            srcs = sorted({r["source"] for r in rs})
            sev = min((orphan_sev.get(s, "warning") for s in srcs), key=lambda s: SEV_ORDER[s])
            self.add_issue("not_in_anchor", sev, "Identity", k, None,
                           f"{rs[0]['norm']['_name']['display']}: not found in {self.label(self.anchor)}",
                           f"Appears in {', '.join(self.label(s) for s in srcs)} ({len(rs)} records) but matches no one in the "
                           f"{self.label(self.anchor)}. Could be new, a typo, or a record that shouldn't exist.",
                           f"Add them to the {self.label(self.anchor)}, or fix the record so it matches an existing one.",
                           [self.ev(x) for x in rs][:8], fp=("orphan", nk, ",".join(srcs)))

        # 4. record-level problems -----------------------------------------
        for r in recs:
            for field, sev, msg in r["problems"]:
                self.add_issue("record", sev, "Data quality", r["entity"], field,
                               f"{self.label(r['source'])}: {msg}",
                               f"{r['file']}, {r['locator']}, field '{field}'.",
                               "Correct the value in the source system and re-ingest.",
                               [self.ev(r, field)], fp=("record", r["source"], r["id"] if not r["key"] else r["key"], field, r["raw"].get(field)))
            dup_fields = cfg["sources"][r["source"]].get("duplicate_check")
        self._duplicate_records()

        # 5. reconcile attributes ------------------------------------------
        for e in ents.values():
            e["attrs"] = {}
            for attr, spec in cfg["attributes"].items():
                e["attrs"][attr] = self.reconcile(e, attr, spec)
            nm = e["attrs"].get("name", {}).get("value")
            e["display"] = nm if nm else (e["name"]["display"] if e.get("name") else e["key"])

        # 6. rules -------------------------------------------------------
        for rule in cfg.get("rules", []):
            fn = rule_lib.REGISTRY.get(rule["type"])
            if fn:
                fn(self, rule)

        self._persist()
        return self.summary()

    # ------------------------------------------------------------ helpers
    def rank(self, nm, hints):
        out = []
        for k, e in self.ents.items():
            s, how = name_similarity(nm, e["name"])
            if s < self.floor - 0.1:
                continue
            bonus = 0
            for h, v in hints.items():
                anchor_field = self.cfg["attributes"].get(h, {}).get("from", {}).get(self.anchor)
                if v and anchor_field:
                    av = {ar["norm"].get(anchor_field) for ar in e["records"][self.anchor]}
                    bonus += 0.01 if v in av else 0
            out.append((round(min(1.0, s) + bonus, 3), k, how))
        out.sort(key=lambda t: -t[0])
        return [(min(1.0, round(s, 3)), k, how) for s, k, how in out]

    def link(self, r, key, method, score, status):
        r["entity"], r["method"], r["score"], r["mstatus"] = key, method, score, status
        self.ents[key]["records"][r["source"]].append(r)

    def ev(self, r, field=None):
        d = {"record_id": r["id"], "source": r["source"], "label": self.label(r["source"]), "file": r["file"], "locator": r["locator"]}
        if field:
            d["field"] = field
            d["raw"] = display(r["raw"].get(field))
            d["value"] = display(r["norm"].get(field))
        else:
            d["raw"] = {k: v for k, v in r["raw"].items() if not k.startswith("_")}
        return d

    def add_issue(self, rule, severity, category, entity_key, attribute, title, detail, action, evidence,
                  fp, options=None, data=None):
        fps = "|".join(display(x) for x in fp)
        fph = hashlib.sha1(fps.encode()).hexdigest()[:16]
        if fph in self.issues:
            return
        self.issues[fph] = {"fingerprint": fph, "rule": rule, "severity": severity, "category": category,
                            "entity_key": entity_key, "attribute": attribute, "title": title, "detail": detail,
                            "action": action, "data": {"evidence": evidence, "options": options or ["acknowledge", "dismiss"], **(data or {})}}

    def _duplicate_records(self):
        for source, scfg in self.cfg["sources"].items():
            rk = scfg.get("record_key")
            if rk and source != self.anchor:
                seen = defaultdict(list)
                for r in self.by_source.get(source, []):
                    if r["key"]:
                        seen[r["key"]].append(r)
                for k, rs in seen.items():
                    if len(rs) > 1:
                        self.add_issue("duplicate_key", "warning", "Data quality", rs[0]["entity"], rk,
                                       f"{self.label(source)}: {rk} {k} appears {len(rs)} times",
                                       "The same record identifier is used by more than one row.",
                                       "Check whether one row is a duplicate export.", [self.ev(x) for x in rs], fp=("dupkey", source, k))
            fields = scfg.get("duplicate_check")
            if fields:
                seen = defaultdict(list)
                for r in self.by_source.get(source, []):
                    sig = tuple(display(r["norm"].get(f)) for f in fields)
                    if all(sig):
                        seen[sig].append(r)
                for sig, rs in seen.items():
                    if len(rs) > 1:
                        self.add_issue("duplicate_record", "critical", scfg.get("duplicate_category", "Duplicates"), rs[0]["entity"], None,
                                       scfg.get("duplicate_title", "Possible duplicate entry") + f" ({len(rs)} identical rows in {self.label(source)})",
                                       f"Rows match on {', '.join(fields)} but have different record IDs ({', '.join(str(x['key']) for x in rs)}). Possible double payment.",
                                       "Confirm with payroll whether this was paid twice.", [self.ev(x) for x in rs], fp=("duprec", source) + sig)

    def reconcile(self, e, attr, spec):
        vals = []
        for source, field in spec["from"].items():
            for r in e["records"].get(source, []):
                v = r["norm"].get(field)
                if v is None or v == "":
                    continue
                vals.append({"source": source, "record_id": r["id"], "raw": display(r["raw"].get(field) if field != "_name" else v),
                             "value": display(v), "obj": v, "mstatus": r["mstatus"]})
        res = {"value": None, "status": "MISSING", "authority": None, "sources": {}, "evidence": [], "note": None}
        if not vals:
            return res
        all_vals = vals
        vals = [v for v in vals if v["mstatus"] != "review"]   # provisional links are shown, never trusted
        if not vals:
            res["evidence"] = [{k: v[k] for k in ("source", "record_id", "raw", "value", "mstatus")} for v in all_vals]
            return res
        per_source = defaultdict(list)
        for v in vals:
            if v["value"] not in per_source[v["source"]]:
                per_source[v["source"]].append(v["value"])
        res["sources"] = dict(per_source)
        res["evidence"] = [{k: v[k] for k in ("source", "record_id", "raw", "value", "mstatus")} for v in all_vals]
        authority = next((s for s in spec.get("authority", []) if s in per_source), None)
        res["authority"] = authority
        auth_val = per_source[authority][0] if authority else max({v["value"] for v in vals}, key=lambda x: sum(1 for v in vals if v["value"] == x))

        if spec.get("compare") == "person_name":
            objs = {v["value"]: v["obj"] for v in vals}
            ref = objs[auth_val]
            distinct = [n for n in objs if name_similarity(objs[n], ref)[0] < self.auto]
            conflict = bool(distinct)
        else:
            distinct = sorted({v["value"] for v in vals})
            conflict = len(distinct) > 1
        res["value"] = auth_val
        if not conflict:
            res["status"] = "CONFIRMED" if len(per_source) > 1 else "SINGLE_SOURCE"
            if spec.get("compare") == "person_name" and len({v['value'] for v in vals}) > 1:
                res["note"] = "Spelled differently across systems; treated as the same name (" + ", ".join(sorted({v['value'] for v in vals})) + ")"
            return res
        # conflict ----------------------------------------------------------
        options = sorted({v["value"] for v in vals})
        fp = ("conflict", e["key"], attr) + tuple(f"{s}={'/'.join(sorted(per_source[s]))}" for s in sorted(per_source))
        parts = " · ".join(f"{self.label(s)}: {', '.join(pretty(x) for x in per_source[s])}" for s in per_source)
        n_disagree = sum(1 for s in per_source if auth_val not in per_source[s])
        note = ""
        words = attr.replace("_", " ")
        if authority:
            note = f" We trust {self.label(authority)} for {words}, so we're using {pretty(auth_val)} until you decide."
            if n_disagree > len(per_source) / 2:
                note += f" Heads up: {n_disagree} of {len(per_source)} systems disagree with it, so {self.label(authority)} may be the one that's out of date."
        self.add_issue("conflict", spec.get("severity", "warning"), spec.get("category", "Consistency"), e["key"], attr,
                       f"Systems disagree on {words_for(attr)}",
                       parts + "." + note,
                       "Pick the right value below, then fix it in the system that's wrong.",
                       [{"source": v["source"], "label": self.label(v["source"]), "record_id": v["record_id"], "field": attr,
                         "raw": v["raw"], "value": v["value"]} for v in vals][:10],
                       fp=fp, options=options)
        fph = hashlib.sha1("|".join(display(x) for x in fp).encode()).hexdigest()[:16]
        d = self.decisions.get(fph)
        if d and d["decision"] == "choose" and d["value"]:
            res.update(value=d["value"], status="RESOLVED", note=f"Chosen by {d['reviewer'] or 'reviewer'}: {d['note'] or ''}".strip())
        else:
            res.update(status="CONFLICT", note=note.strip())
        res["fingerprint"] = fph
        return res

    def attr(self, e, name):
        return e["attrs"].get(name, {}).get("value")

    # ------------------------------------------------------------ rules learned from decisions
    def auto_rule_for(self, i):
        if i["severity"] == "critical":       # guardrail: urgent items always need a person
            return None
        key = pattern_key(i)
        for r in self.auto_rules:
            if r["pattern"] != key:
                continue
            c = r["cond"]
            if c:
                ent = self.ents.get(i["entity_key"])
                v = ((ent or {}).get("attrs", {}).get(c["attr"], {}) or {}).get("value")
                if v is None or simplify(str(v)) != simplify(str(c["value"])):
                    continue
            return r
        return None

    # ------------------------------------------------------------ persistence
    def _persist(self):
        st = self.store
        ts = now()
        existing = {i["fingerprint"]: i for i in st.q("SELECT fingerprint, active, status FROM issues")}
        with st.lock:
            db = st.db
            for r in self.recs:
                db.execute("UPDATE records SET norm_json=?, problems_json=?, entity_key=?, match_method=?, match_score=?, match_status=? WHERE id=?",
                           (dumps(r["norm"]), dumps(r["problems"]), r["entity"], r["method"], r["score"], r["mstatus"], r["id"]))
            db.execute("DELETE FROM entities")
            db.execute("DELETE FROM evidence")
            open_by_ent = defaultdict(list)
            applied = defaultdict(int)
            for fp, i in self.issues.items():
                d = self.decisions.get(fp)
                status = {"choose": "resolved", "confirm": "resolved", "reject": "resolved",
                          "acknowledge": "acknowledged", "dismiss": "dismissed"}.get(d["decision"], "open") if d else "open"
                if not d:      # no decision on this exact issue: does a rule learned from earlier decisions cover it?
                    r = self.auto_rule_for(i)
                    if r:
                        status = "auto"
                        i["data"]["auto_rule"] = {"id": r["id"], "label": r["label"], "action": r["action"]}
                        applied[r["id"]] += 1
                        if existing.get(fp, {}).get("status") != "auto":
                            db.execute("INSERT INTO events(at, kind, entity_key, fingerprint, message) VALUES (?,?,?,?,?)",
                                       (ts, "auto_resolved", i["entity_key"], fp, f"Auto-resolved by rule \"{r['label']}\": {i['title']}"))
                i["status"] = status
                if status == "open" and i["entity_key"]:
                    open_by_ent[i["entity_key"]].append(i["severity"])
                if fp in existing:
                    db.execute("""UPDATE issues SET rule=?, severity=?, category=?, entity_key=?, attribute=?, title=?, detail=?,
                                  action=?, data_json=?, status=?, last_seen=?, active=1 WHERE fingerprint=?""",
                               (i["rule"], i["severity"], i["category"], i["entity_key"], i["attribute"], i["title"], i["detail"],
                                i["action"], dumps(i["data"]), status, ts, fp))
                    if not existing[fp]["active"]:
                        db.execute("INSERT INTO events(at, kind, entity_key, fingerprint, message) VALUES (?,?,?,?,?)",
                                   (ts, "issue_reopened", i["entity_key"], fp, f"Re-detected: {i['title']}"))
                else:
                    db.execute("""INSERT INTO issues(fingerprint, rule, severity, category, entity_key, attribute, title, detail, action,
                                  data_json, status, first_seen, last_seen, active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                               (fp, i["rule"], i["severity"], i["category"], i["entity_key"], i["attribute"], i["title"], i["detail"],
                                i["action"], dumps(i["data"]), status, ts, ts))
                    db.execute("INSERT INTO events(at, kind, entity_key, fingerprint, message) VALUES (?,?,?,?,?)",
                               (ts, "issue_detected", i["entity_key"], fp, f"[{i['severity']}] {i['title']}"))
            for fp, old in existing.items():
                if fp not in self.issues and old["active"]:
                    row = db.execute("SELECT entity_key, title FROM issues WHERE fingerprint=?", (fp,)).fetchone()
                    db.execute("UPDATE issues SET active=0 WHERE fingerprint=?", (fp,))
                    db.execute("INSERT INTO events(at, kind, entity_key, fingerprint, message) VALUES (?,?,?,?,?)",
                               (ts, "issue_cleared", row["entity_key"], fp, f"No longer present in the data: {row['title']}"))
            for r in self.auto_rules:
                db.execute("UPDATE auto_rules SET applied=? WHERE id=?", (applied.get(r["id"], 0), r["id"]))
            for e in self.ents.values():
                sevs = open_by_ent.get(e["key"], [])
                worst = min(sevs, key=lambda s: SEV_ORDER[s]) if sevs else None
                attrs_out = {a: {k: v for k, v in d.items() if k != "evidence"} for a, d in e["attrs"].items()}
                db.execute("INSERT INTO entities(key, type, display_name, status, attrs_json, sources_json, open_issues, worst) VALUES (?,?,?,?,?,?,?,?)",
                           (e["key"], self.cfg["entity"]["type"], e["display"], e["status"], dumps(attrs_out),
                            dumps({s: len(rs) for s, rs in e["records"].items() if rs}), len([s for s in sevs if s != "info"]), worst))
                for a, d in e["attrs"].items():
                    for v in d.get("evidence", []):
                        db.execute("INSERT INTO evidence(entity_key, attribute, source, record_id, raw_value, value) VALUES (?,?,?,?,?,?)",
                                   (e["key"], a, v["source"], v["record_id"], v["raw"], v["value"]))
            n_open = sum(1 for i in self.issues.values() if i["status"] == "open")
            db.execute("INSERT INTO events(at, kind, entity_key, fingerprint, message) VALUES (?,?,?,?,?)",
                       (ts, "process", None, None, f"Processed {len(self.recs)} records into {len(self.ents)} entities; {n_open} open issues"))
            db.commit()

    # ------------------------------------------------------------ read models
    def summary(self):
        st = self.store
        ents = st.q("SELECT key, status, open_issues, worst, attrs_json FROM entities")
        recs = st.q("SELECT r.source, r.match_status FROM records r JOIN files f ON f.id=r.file_id WHERE f.active=1")
        issues = st.q("SELECT severity, category, status FROM issues WHERE active=1")
        open_ = [i for i in issues if i["status"] == "open"]
        attr_total = attr_ok = 0
        for e in ents:
            for a in json.loads(e["attrs_json"]).values():
                if a["status"] != "MISSING":
                    attr_total += 1
                    attr_ok += a["status"] in ("CONFIRMED", "SINGLE_SOURCE", "RESOLVED")
        linked = sum(1 for r in recs if r["match_status"] in ("anchor", "auto", "confirmed"))
        verified = [e for e in ents if e["status"] == "verified"]
        clean = [e for e in verified if e["worst"] not in ("critical",)]
        by_source = defaultdict(int)
        for r in recs:
            by_source[r["source"]] += 1
        return {
            "client": self.cfg["client"], "as_of": self.as_of().isoformat(),
            "entity_label": self.cfg["entity"].get("label", "Entity"), "anchor_label": self.label(self.anchor),
            "views": list(self.cfg.get("views", {}).keys()),
            "generic_views": [v for v, t in (("expiring", "expiration"), ("totals", "aggregate_compare"))
                              if any(r["type"] == t for r in self.cfg.get("rules", []))],
            "entities": len(verified), "unverified_entities": len(ents) - len(verified),
            "records": len(recs), "records_by_source": dict(by_source),
            "sources_loaded": len(by_source), "sources_configured": len(self.cfg["sources"]),
            "metrics": {
                "records_linked": round(100 * linked / len(recs)) if recs else None,
                "fields_agreeing": round(100 * attr_ok / attr_total) if attr_total else None,
                "entities_without_critical": round(100 * len(clean) / len(verified)) if verified else None,
            },
            "open_issues": len(open_),
            "by_severity": {s: sum(1 for i in open_ if i["severity"] == s) for s in ("critical", "warning", "info")},
            "by_category": {c: sum(1 for i in open_ if i["category"] == c) for c in sorted({i["category"] for i in open_})},
            "resolved": sum(1 for i in issues if i["status"] != "open"),
        }


def source_is_grid(scfg):
    return scfg.get("parser") == "weekly_grid"


# ---------------------------------------------------------------- human decisions
def pattern_key(i) -> str:
    """Issues of the same kind: same check, same field (e.g. every 'Not found in CDL report')."""
    return f"{i['rule']}|{i.get('attribute') or ''}"


def generic_title(t: str) -> str:
    t = re.sub(r"\(\d{1,2}/\d{1,2}[–-]\d{1,2}/\d{1,2}\)", "", t)
    t = re.sub(r"\d[\d,.]*", "#", t)
    return re.sub(r"\s+", " ", t).strip()


def similar_options(eng, issue_id: int):
    """What a rule made from this decision would cover: all issues of this kind, or only where a profile field matches."""
    st = eng.store
    i = st.one("SELECT * FROM issues WHERE id=?", (issue_id,))
    if not i:
        raise KeyError(issue_id)
    if i["severity"] == "critical":
        return {"allowed": False, "reason": "Urgent issues always need a person, so they can't be auto-resolved.", "options": []}
    key = pattern_key(i)
    ents = {e["key"]: json.loads(e["attrs_json"]) for e in st.q("SELECT key, attrs_json FROM entities")}
    same = [x for x in st.q("SELECT * FROM issues WHERE active=1 AND severity!='critical'") if pattern_key(x) == key]
    others = [x for x in same if x["id"] != i["id"] and x["status"] == "open"]
    title = generic_title(i["title"])
    opts = [{"condition": None, "label": f"Every \"{title}\" issue", "count": len(others)}]
    mine = ents.get(i["entity_key"], {})
    for attr, a in mine.items():
        v = a.get("value")
        if not v or attr == "name" or re.match(r"^\d{4}-\d{2}-\d{2}", str(v)):
            continue      # dates make poor rule conditions
        distinct = {str(e.get(attr, {}).get("value")) for e in ents.values() if e.get(attr, {}).get("value")}
        if len(distinct) < 2 or len(distinct) > 12 or len(distinct) >= max(3, len(ents) * .6):
            continue      # only fields that describe a group (role, facility, status), not IDs or dates
        n = sum(1 for x in others if str(ents.get(x["entity_key"], {}).get(attr, {}).get("value")) == str(v))
        opts.append({"condition": {"attr": attr, "value": v},
                     "label": f"Only where {attr.replace('_', ' ')} is {v}", "count": n})
    return {"allowed": True, "pattern": key, "title": title, "options": opts}


def create_auto_rule(eng, issue_id: int, condition, action: str, reviewer=None):
    st = eng.store
    i = st.one("SELECT * FROM issues WHERE id=?", (issue_id,))
    if not i:
        raise KeyError(issue_id)
    if i["severity"] == "critical":
        raise ValueError("Urgent issues can't be auto-resolved")
    if action not in ("dismiss", "acknowledge"):
        raise ValueError("unknown action")
    verb = "Not a problem" if action == "dismiss" else "Handled"
    label = f"{verb}: {generic_title(i['title'])}" + (f" when {condition['attr'].replace('_', ' ')} is {condition['value']}" if condition else "")
    st.x("INSERT INTO auto_rules(pattern, condition_json, action, label, created_by, created_at, from_issue, active) VALUES (?,?,?,?,?,?,?,1)",
         (pattern_key(i), json.dumps(condition) if condition else None, action, label, reviewer, now(), i["fingerprint"]))
    st.event("rule", f"{reviewer or 'Reviewer'} created a rule: {label}", i["entity_key"], i["fingerprint"])
    return eng.process()


def resolve_issue(eng: Engine, issue_id: int, decision: str, value=None, note=None, reviewer=None):
    st = eng.store
    i = st.one("SELECT * FROM issues WHERE id=?", (issue_id,))
    if not i:
        raise KeyError(issue_id)
    data = json.loads(i["data_json"])
    fp = i["fingerprint"]
    if decision == "reopen":
        st.x("UPDATE decisions SET active=0 WHERE fingerprint=?", (fp,))
        if i["status"] == "auto":    # undo an auto-resolution: this one stays open even though the rule matches
            st.x("INSERT INTO decisions(fingerprint, decision, value, note, reviewer, decided_at, active) VALUES (?,?,?,?,?,?,1)",
                 (fp, "keep_open", None, note, reviewer, now()))
        if i["rule"] == "match":
            st.x("DELETE FROM match_overrides WHERE source=? AND name_key=?", (data["source"], data["name_key"]))
        st.event("decision", f"Reopened: {i['title']}" + (f" — {note}" if note else ""), i["entity_key"], fp)
        return eng.process()
    if decision not in ("choose", "confirm", "reject", "acknowledge", "dismiss"):
        raise ValueError("unknown decision")
    if decision == "choose" and value not in data.get("options", []):
        raise ValueError("value must be one of the observed values")
    st.x("UPDATE decisions SET active=0 WHERE fingerprint=?", (fp,))
    st.x("INSERT INTO decisions(fingerprint, decision, value, note, reviewer, decided_at, active) VALUES (?,?,?,?,?,?,1)",
         (fp, decision, value, note, reviewer, now()))
    if i["rule"] == "match" and decision in ("confirm", "reject"):
        target = data["candidate"] if decision == "confirm" else "__none__"
        st.x("""INSERT INTO match_overrides(source, name_key, entity_key, reviewer, decided_at) VALUES (?,?,?,?,?)
                ON CONFLICT(source, name_key) DO UPDATE SET entity_key=excluded.entity_key, reviewer=excluded.reviewer, decided_at=excluded.decided_at""",
             (data["source"], data["name_key"], target, reviewer, now()))
    verb = {"choose": f"chose '{value}'", "confirm": "confirmed the match", "reject": "rejected the match",
            "acknowledge": "acknowledged", "dismiss": "dismissed as not an issue"}[decision]
    st.event("decision", f"{reviewer or 'Reviewer'} {verb}: {i['title']}" + (f" — {note}" if note else ""), i["entity_key"], fp)
    return eng.process()
