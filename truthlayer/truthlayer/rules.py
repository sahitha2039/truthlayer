"""Generic rule types. A client config instantiates them with its own fields and thresholds.

Each rule receives the engine (entities, records, add_issue, as_of) and its config block.
None of these functions mention nurses, licenses or payroll; those words only appear in YAML.
"""
from __future__ import annotations

from collections import defaultdict
import re
from datetime import date, timedelta

from .normalize import simplify


def _d(v):
    try:
        return date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def _when(eng, e, cond):
    for attr, allowed in (cond or {}).items():
        if eng.attr(e, attr) not in allowed:
            return False
    return True


def _verified(eng):
    return [e for e in eng.ents.values() if e["status"] == "verified"]


def presence(eng, rule):
    for e in _verified(eng):
        for src, spec in rule["expect"].items():
            if src not in eng.by_source:   # source not loaded at all: don't blame every entity
                continue
            if _when(eng, e, spec.get("when")) and not e["records"].get(src):
                eng.add_issue(rule["id"], spec.get("severity", "warning"), spec.get("category", "Completeness"), e["key"], None,
                              f"{spec.get('message', 'Missing from ' + eng.label(src))}",
                              f"{e['display']} ({e['key']}) has no records in {eng.label(src)}.",
                              f"Check {eng.label(src)} for this person under a different name or ID.",
                              [], fp=(rule["id"], e["key"], src))


def required_attribute(eng, rule):
    for e in _verified(eng):
        if _when(eng, e, rule.get("when")) and e["attrs"][rule["attribute"]]["status"] == "MISSING":
            eng.add_issue(rule["id"], rule.get("severity", "warning"), rule.get("category", "Credentials"), e["key"], rule["attribute"],
                          rule["message"], f"{e['display']} has role {eng.attr(e, 'role')} but no {rule['attribute'].replace('_', ' ')} in any source.",
                          "Record the credential in the source system.", [], fp=(rule["id"], e["key"]))


def expiration(eng, rule):
    today = eng.as_of()
    for e in eng.ents.values():
        a = e["attrs"][rule["attribute"]]
        d = _d(a["value"])
        if not d:
            continue
        days = (d - today).days
        label = rule.get("label", rule["attribute"])
        conflict = " (sources disagree on this date — see conflict)" if a["status"] == "CONFLICT" else ""
        if days < rule.get("critical_days", 0):
            eng.add_issue(rule["id"], "critical", rule.get("category", "Credentials"), e["key"], rule["attribute"],
                          f"{label} expired {-days} days ago", f"{label} for {e['display']} expired on {d}{conflict}.",
                          "Remove from the schedule until renewed and verified.", [], fp=(rule["id"], e["key"], d, "expired"),
                          data={"days": days})
        elif days <= rule.get("warn_days", 60):
            eng.add_issue(rule["id"], "warning", rule.get("category", "Credentials"), e["key"], rule["attribute"],
                          f"{label} expires in {days} days", f"{label} for {e['display']} expires on {d}{conflict}.",
                          "Start renewal now.", [], fp=(rule["id"], e["key"], d, "soon"), data={"days": days})


def staleness(eng, rule):
    today = eng.as_of()
    for e in eng.ents.values():
        d = _d(e["attrs"][rule["attribute"]]["value"])
        if d and (today - d).days > rule["max_age_days"]:
            eng.add_issue(rule["id"], rule.get("severity", "warning"), rule.get("category", "Credentials"), e["key"], rule["attribute"],
                          f"{rule.get('label', rule['attribute'])} is {(today - d).days} days old",
                          f"Last verified {d}; policy is every {rule['max_age_days']} days.",
                          "Re-verify with the issuing authority.", [], fp=(rule["id"], e["key"], d))


def activity_after_expiry(eng, rule):
    for e in eng.ents.values():
        a = e["attrs"][rule["attribute"]]
        exp = _d(a["value"])
        if not exp:
            continue
        hits = []
        for src, spec in rule["activity"].items():
            for r in e["records"].get(src, []):
                d = _d(r["norm"].get(spec["date"]))
                worked = r["norm"].get("hours", 1) if "hours" in r["norm"] else 1
                if d and d > exp and worked:
                    hits.append((src, spec["label"], d, r))
        if hits:
            first = min(h[2] for h in hits)
            others = ""
            if a["status"] == "CONFLICT":
                others = " Sources disagree on the expiration date: " + "; ".join(f"{eng.label(s)} {', '.join(v)}" for s, v in a["sources"].items()) + "."
            eng.add_issue(rule["id"], rule.get("severity", "critical"), rule.get("category", "Credentials"), e["key"], rule["attribute"],
                          rule.get("title", "Worked after the expiry date") + f" ({exp})",
                          f"{e['display']} has {len(hits)} {', '.join(sorted({h[1] for h in hits}))} record(s) after {exp}, starting {first}.{others}",
                          "Verify the credential immediately; this is a compliance exposure.",
                          [eng.ev(h[3]) for h in sorted(hits, key=lambda h: h[2])][:8], fp=(rule["id"], e["key"], exp, len(hits)))


def aggregate_compare(eng, rule):
    """Compare a measure summed over periods in source A with a daily measure in source B."""
    A, B, tol = rule["a"], rule["b"], rule.get("tolerance", 0)
    b_dates = {r["norm"].get(B["date"]) for r in eng.by_source.get(B["source"], []) if r["norm"].get(B["date"])}
    if not b_dates or not eng.by_source.get(A["source"]):
        return
    lo, hi = _d(min(b_dates)), _d(max(b_dates))
    periods = set()
    for r in eng.by_source[A["source"]]:
        s, t = _d(r["norm"].get(A["start"])), _d(r["norm"].get(A["end"]))
        if not (s and t and t >= s):
            continue
        if rule.get("coverage", "every_day") == "every_day":   # B lists every day (e.g. a schedule grid, OFF included)
            ok = all((s + timedelta(i)).isoformat() in b_dates for i in range((t - s).days + 1))
        else:                                                  # B only has rows when something happened
            slack = timedelta(days=rule.get("slack_days", 0))
            ok = lo - slack <= s and t <= hi + slack
        if ok:
            periods.add((s, t))
    if not periods:
        eng.add_issue(rule["id"], "info", rule.get("category", "Staffing"), None, None,
                      f"{rule['label']}: no overlapping periods",
                      f"{eng.label(A['source'])} periods do not line up with the dates covered by {eng.label(B['source'])}, so nothing could be compared.",
                      "Load files that cover the same dates.", [], fp=(rule["id"], "nooverlap"))
        return
    eng.compare_periods = sorted(periods)
    for e in eng.ents.values():
        if e["status"] == "verified" and not _when(eng, e, rule.get("when")):
            continue
        for (s, t) in sorted(periods):
            a_by, b_by, a_recs, b_recs = defaultdict(float), defaultdict(float), [], []
            for r in e["records"].get(A["source"], []):
                if (_d(r["norm"].get(A["start"])), _d(r["norm"].get(A["end"]))) == (s, t):
                    a_by[r["norm"].get(A.get("group"))] += r["norm"].get(A["measure"]) or 0
                    a_recs.append(r)
            for r in e["records"].get(B["source"], []):
                d = _d(r["norm"].get(B["date"]))
                if d and s <= d <= t and (r["norm"].get(B["measure"]) or 0):
                    b_by[r["norm"].get(B.get("group"))] += r["norm"].get(B["measure"]) or 0
                    b_recs.append(r)
            ta, tb = sum(a_by.values()), sum(b_by.values())
            if not a_recs and not b_recs:
                continue
            per = f"{s:%m/%d}–{t:%m/%d}"
            ev = [eng.ev(r) for r in a_recs] + [eng.ev(r) for r in b_recs if r["norm"].get(B["measure"])][:7]
            data = {"period": [s.isoformat(), t.isoformat()], "a": ta, "b": tb}
            av, bv, pre, u = A.get("verb", eng.label(A["source"])), B.get("verb", eng.label(B["source"])), rule.get("prefix", ""), rule.get("unit", "")
            low = lambda w, spec: w.lower() if "verb" in spec else w      # verbs read as words; system names keep their case
            q = lambda n: f"{pre}{n:,.10g}{u}"
            if abs(ta - tb) > tol:
                if not a_recs:
                    title = f"{bv} {q(tb)} but not {low(av, A)} ({per})" if "verb" in A else f"In {bv} ({q(tb)}) but not in {av} ({per})"
                elif not b_recs:
                    title = f"{av} {q(ta)} but not {low(bv, B)} ({per})" if "verb" in B else f"In {av} ({q(ta)}) but not in {bv} ({per})"
                else:
                    title = f"{av} {q(ta)} vs {low(bv, B)} {q(tb)} ({per})"
                eng.add_issue(rule["id"], rule.get("severity", "warning"), rule.get("category", "Staffing"), e["key"], None, title,
                              f"{eng.label(A['source'])}: {q(ta)} · {eng.label(B['source'])}: {q(tb)} · difference {ta - tb:+,.10g}{u} for {e['display']}.",
                              rule.get("action", "Confirm which number is right before it is reported."), ev, fp=(rule["id"], e["key"], s, ta, tb), data=data)
            else:
                ga = {k: v for k, v in a_by.items() if v}
                gb = {k: v for k, v in b_by.items() if v}
                if ga and gb and set(ga) != set(gb):
                    eng.add_issue(rule["id"], "warning", rule.get("category", "Staffing"), e["key"], None,
                                  f"Booked to a different {(A.get('group') or 'group').split('_')[0]} ({per})",
                                  f"Totals agree ({q(ta)}) but {eng.label(A['source'])} books them to {', '.join(map(str, ga))} and {eng.label(B['source'])} to {', '.join(map(str, gb))}.",
                                  "Confirm where the hours were worked.", ev, fp=(rule["id"], e["key"], s, "group", tuple(ga), tuple(gb)), data=data)


def duplicate_entity(eng, rule):
    pairs = defaultdict(set)
    for attr in rule["match_on"]:
        idx = defaultdict(list)
        for e in _verified(eng):
            v = eng.attr(e, attr)
            if v:
                idx[v].append(e["key"])
        for v, keys in idx.items():
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    pairs[tuple(sorted((keys[i], keys[j])))].add(f"{attr.replace('_', ' ')} {v}")
    for (k1, k2), shared in pairs.items():
        e1, e2 = eng.ents[k1], eng.ents[k2]
        eng.add_issue(rule["id"], rule.get("severity", "warning"), "Identity", k1, None,
                      f"Possible duplicate: {e1['display']} ({k1}) and {e2['display']} ({k2})",
                      f"They share {', '.join(sorted(shared))}. One person may have two records, which double-counts headcount and splits their history.",
                      "Merge in the source system, or dismiss if they are different people.",
                      [], fp=(rule["id"], k1, k2), data={"related": [k2]})


def date_order(eng, rule):
    for r in eng.by_source.get(rule["source"], []):
        s, t = _d(r["norm"].get(rule["start"])), _d(r["norm"].get(rule["end"]))
        if not s or not t:
            continue
        if t < s:
            eng.add_issue(rule["id"], rule.get("severity", "warning"), "Data quality", r["entity"], rule["end"],
                          f"{eng.label(rule['source'])}: period ends before it starts", f"{s} → {t} in {r['file']}, {r['locator']}.",
                          "Correct the period dates.", [eng.ev(r)], fp=(rule["id"], r["id"], s, t))
        elif rule.get("expected_days") and (t - s).days + 1 != rule["expected_days"]:
            eng.add_issue(rule["id"], "info", "Data quality", r["entity"], rule["end"],
                          f"{eng.label(rule['source'])}: period is {(t - s).days + 1} days, expected {rule['expected_days']}",
                          f"{s} → {t} in {r['file']}, {r['locator']}.", "Check whether this is a partial period.", [eng.ev(r)],
                          fp=(rule["id"], r["id"], s, t))


def unusual_value(eng, rule):
    """A status-like field that isn't the normal value: SUSPENDED licence, Terminated employee, Closed account."""
    expected = {simplify(x) for x in rule["expected"]}
    crit = re.compile(rule["critical_pattern"], re.I) if rule.get("critical_pattern") else None
    for e in eng.ents.values():
        a = e["attrs"].get(rule["attribute"], {})
        v = a.get("value")
        if v and simplify(v) not in expected:
            sev = "critical" if crit and crit.search(str(v)) else rule.get("severity", "warning")
            eng.add_issue(rule["id"], sev, rule.get("category", "Status"), e["key"], rule["attribute"],
                          f"{rule.get('label', rule['attribute'])} is {v}",
                          f"{e['display']}'s {rule.get('label', rule['attribute']).lower()} is '{v}'. Normally it's {', '.join(rule['expected'])}.",
                          "Check whether they should still be active, and fix the record if not.", [], fp=(rule["id"], e["key"], v))


REGISTRY = {"unusual_value": unusual_value,

    "presence": presence, "required_attribute": required_attribute, "expiration": expiration,
    "staleness": staleness, "activity_after_expiry": activity_after_expiry,
    "aggregate_compare": aggregate_compare, "duplicate_entity": duplicate_entity, "date_order": date_order,
}
