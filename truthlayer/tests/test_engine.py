"""Run: python -m pytest -q   (or: python tests/test_engine.py)

1. Every problem planted in sample_data/ must be flagged.
2. The same data, deliberately mangled the way real exports get mangled, must still load
   and produce the same core findings.
"""
import csv, io, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yaml
from truthlayer.engine import Engine, resolve_issue
from truthlayer.store import Store

ROOT = os.path.join(os.path.dirname(__file__), "..")
CFG = yaml.safe_load(open(os.path.join(ROOT, "clients", "harborview.yaml")))


def fresh():
    st = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    st.set_setting("as_of", "2026-10-04")
    return Engine(CFG, st), st


def load(eng, files):
    for name, data in files.items():
        eng.ingest(data, name)
    return eng.process()


def sample_files():
    d = os.path.join(ROOT, "sample_data")
    return {f: open(os.path.join(d, f), "rb").read() for f in os.listdir(d)}


def titles(st):
    return [(i["entity_key"], i["title"]) for i in st.q("SELECT * FROM issues WHERE active=1")]


def has(st, key, text):
    return any(k == key and text.lower() in t.lower() for k, t in titles(st))


EXPECTED = [
    ("E204", "license expiration differs"),
    ("E205", "license expired"),
    ("E205", "active after credential expired"),
    ("E203", "expires in 42 days"),
    ("E209", "verification is"),
    ("E208", "no record from the licensing service"),
    ("E208", "scheduled 32 h but not paid"),
    ("E206", "facility differs"),
    ("E206", "above the expected maximum"),
    ("E207", "role differs"),
    ("E207", "name differs"),
    ("E202", "identical rows"),
    ("E203", "paid 44 h vs scheduled 36 h"),
    ("E201", "possible match"),
    ("E211", "possible duplicate"),
    ("E210", "not a valid date"),
    ("U-diaz-carmen", "not on the hr roster"),
]


def check_expected(st):
    missing = [e for e in EXPECTED if not has(st, *e)]
    assert not missing, f"not flagged: {missing}"


def test_sample_catches_everything():
    eng, st = fresh()
    s = load(eng, sample_files())
    check_expected(st)
    assert s["entities"] == 12 and s["unverified_entities"] == 1
    # Marc Bell (schedule) must auto-link to Marcus Bell via nickname, with no review issue
    recs = st.q("SELECT match_status, match_method FROM records WHERE entity_key='E202' AND source='schedule'")
    assert recs and all(r["match_status"] == "auto" for r in recs)
    # no false positive: people whose data is clean
    assert not any(k == "E201" and "differs" in t for k, t in titles(st))


def mangle_csv(data, rename, delimiter=",", title_row=None, blank_rows=True, reorder=True):
    rows = list(csv.reader(io.StringIO(data.decode())))
    hdr = [rename.get(h, h) for h in rows[0]]
    body = rows[1:]
    if reorder:
        idx = list(range(len(hdr)))[::-1]
        hdr = [hdr[i] for i in idx]
        body = [[r[i] for i in idx] for r in body]
    out = io.StringIO()
    w = csv.writer(out, delimiter=delimiter)
    if title_row:
        w.writerow([title_row]); w.writerow([])
    w.writerow(hdr + ["Notes"])
    for i, r in enumerate(body):
        w.writerow(r + [""])
        if blank_rows and i == 2:
            w.writerow([])
    return out.getvalue().encode("cp1252")


def test_messy_exports_still_load():
    f = sample_files()
    messy = {
        "Employees_export.csv": mangle_csv(f["hr_roster.csv"], {"employee_id": "Emp ID", "first_name": "First Name", "last_name": "Last Name",
                                                               "job_title": "Position", "license_expiration": "License Exp", "hire_date": "Start Date"},
                                           title_row="HR Export — generated 10/01/2026"),
        "pay.csv": mangle_csv(f["payroll.csv"], {"hours_paid": "Hours", "employee_name": "Employee", "facility_code": "Site"}, delimiter=";"),
        "verif.csv": mangle_csv(f["licenses.csv"], {"name_on_license": "Licensee", "expiration_date": "Expires", "last_verified": "Verified On"}, delimiter="\t"),
        "staff_schedule.pdf": f["staff_schedule.pdf"],
    }
    eng, st = fresh()
    load(eng, messy)
    srcs = {r["source"] for r in st.q("SELECT source FROM files")}
    assert srcs == {"hr", "payroll", "licensing", "schedule"}, srcs
    check_expected(st)


def test_borderless_pdf_and_xlsx():
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(letter))
    c.drawString(40, 560, "HV Bayside — Week of 9/14")
    c.drawString(40, 530, "Staff Role Mon 9/14 Tue 9/15 Wed 9/16 Thu 9/17 Fri 9/18 Sat 9/19 Sun 9/20")
    c.drawString(40, 510, "Sofia Reyes RN 7a-3p 7a-3p OFF 7a-7p OFF 7a-3p OFF")
    c.drawString(40, 490, "Jamie Okafor CNA OFF 3p-11p 3p-11p OFF OFF OFF OFF")
    c.drawString(40, 450, "Shifts: 7a-3p, 3p-11p, 11p-7a are 8 hours. 7a-7p is 12 hours.")
    c.save()
    eng, st = fresh()
    r = eng.ingest(buf.getvalue(), "sched.pdf")
    assert r["records"] == 14, r
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    rows = list(csv.reader(io.StringIO(sample_files()["licenses.csv"].decode())))
    for row in rows:
        ws.append(row)
    x = io.BytesIO(); wb.save(x)
    assert eng.ingest(x.getvalue(), "licenses.xlsx")["source"] == "licensing"


def test_decisions_survive_reprocessing_and_reopen_on_change():
    eng, st = fresh()
    load(eng, sample_files())
    i = st.one("SELECT * FROM issues WHERE rule='conflict' AND attribute='facility' AND entity_key='E206'")
    resolve_issue(eng, i["id"], "choose", "BAYSIDE", "moved to Bayside", "test")
    e = st.one("SELECT attrs_json FROM entities WHERE key='E206'")
    assert '"RESOLVED"' in e["attrs_json"]
    eng.process()
    assert st.one("SELECT status FROM issues WHERE id=?", (i["id"],))["status"] == "resolved"
    # confirm a fuzzy match -> record becomes trusted
    m = st.one("SELECT * FROM issues WHERE rule='match'")
    resolve_issue(eng, m["id"], "confirm", None, None, "test")
    assert st.one("SELECT match_status FROM records WHERE raw_json LIKE '%RAYES%'")["match_status"] == "confirmed"
    # HR gets fixed at the source -> conflict disappears from the data and is logged as cleared
    hr = sample_files()["hr_roster.csv"].decode().replace("HV Riverdale", "Harborview Bayside")
    eng.ingest(hr.encode(), "hr_roster.csv")
    eng.process()
    assert st.one("SELECT active FROM issues WHERE id=?", (i["id"],))["active"] == 0
    assert st.one("SELECT 1 AS x FROM events WHERE kind='issue_cleared' AND entity_key='E206'")
    # HR reverts to the old value -> the identical conflict returns and the recorded decision re-applies
    eng.ingest(sample_files()["hr_roster.csv"], "hr_roster.csv")
    eng.process()
    assert st.one("SELECT status FROM issues WHERE id=?", (i["id"],))["status"] == "resolved"
    # ...but a conflict with *different* values is a new question and opens fresh
    hr = sample_files()["hr_roster.csv"].decode().replace("E203,Priya,Natarajan,RN,Harborview Riverdale", "E203,Priya,Natarajan,RN,Bayside")
    eng.ingest(hr.encode(), "hr_roster.csv")
    eng.process()
    assert st.one("SELECT status FROM issues WHERE rule='conflict' AND attribute='facility' AND entity_key='E203' AND active=1")["status"] == "open"


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
