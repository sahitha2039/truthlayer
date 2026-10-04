"""The guided setup must work for industries it has never seen.

For each sample company we hand the raw CSVs to the setup, accept every suggestion, and
check that the engine then finds the problems planted in the data. No hand-written config.
Run: python -m pytest -q   (or: python tests/test_setup.py)
"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from truthlayer.setup import analyze, build_config, alias_match, cluster_values
from truthlayer.engine import Engine
from truthlayer.store import Store

ROOT = os.path.join(os.path.dirname(__file__), "..")


def auto(folder):
    d = os.path.join(ROOT, folder)
    files = [(f, open(os.path.join(d, f), "rb").read()) for f in sorted(os.listdir(d)) if f.endswith((".csv", ".xlsx", ".pdf"))]
    res = analyze(files)
    cfg = build_config("Test", res["profiles"], res["choices"])
    st = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    st.set_setting("as_of", "2026-10-04")
    eng = Engine(cfg, st)
    for p in res["profiles"]:
        eng.ingest(dict(files)[p["filename"]], p["filename"], forced=p["sid"])
    eng.process()
    titles = [(i["entity_key"], i["title"].lower()) for i in st.q("SELECT * FROM issues WHERE active=1")]
    return res, titles


def has(titles, key, text):
    return any((key is None or k == key) and text in t for k, t in titles)


def test_value_aliases():
    assert alias_match("BYS", ["Harborview Bayside", "Harborview Riverdale"]) == "Harborview Bayside"
    assert alias_match("RVD", ["Harborview Bayside", "Harborview Riverdale"]) == "Harborview Riverdale"
    assert alias_match("RN", ["Registered Nurse", "Licensed Practical Nurse"]) == "Registered Nurse"
    m = cluster_values(["Registered Nurse", "RN", "Licensed Practical Nurse", "Certified Nursing Asst.", "Certified Nursing Assistant"])
    assert m["RN"] == "Registered Nurse" and m["Certified Nursing Asst."] == "Certified Nursing Assistant"
    assert m["Registered Nurse"] != m["Licensed Practical Nurse"]


def test_healthcare_csvs_without_config():
    res, t = auto("sample_data")
    assert res["choices"]["master"] == "hr_roster" and res["choices"]["entity_label"] == "Employee"
    for key, text in [("E205", "expired"), ("E204", "disagree on license expiration"), ("E206", "disagree on facility"),
                      ("E207", "disagree on job title"), ("E201", "same employee"), ("E203", "expires in 42 days"),
                      ("E209", "days old"), ("E202", "duplicate"), ("E211", "possible duplicate"), (None, "carmen diaz")]:
        assert has(t, key, text), (key, text)


def test_healthcare_schedule_pdf_in_guided_setup():
    res, t = auto("sample_data")      # includes staff_schedule.pdf
    assert any(p["grid"] for p in res["profiles"]), "schedule PDF should be read as a grid"
    assert not res["skipped"]
    for key, text in [("E203", "44 h vs staff schedule 36 h"), ("E208", "in staff schedule (32 h) but not in payroll"),
                      ("E205", "active after license expired")]:
        assert has(t, key, text), (key, text)


def test_table_pdf_reads_like_csv():
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table
    import csv, io
    rows = list(csv.reader(open(os.path.join(ROOT, "sample_data", "licenses.csv"))))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=letter).build([Table(rows)])
    from truthlayer.setup import profile_file
    p = profile_file("licenses.pdf", buf.getvalue())
    assert p["rows"] == 10 and p["keys"] == ["license_number"] and not p["grid"]


def test_retail_csvs_without_config():
    res, t = auto("sample_data_retail")
    assert res["choices"]["master"] == "crm" and res["choices"]["entity_label"] == "Customer"
    for key, text in [("C101", "disagree on email"), ("C102", "$990"), ("C103", "possible duplicate"), (None, "tom reed")]:
        assert has(t, key, text), (key, text)


def test_manufacturing_csvs_without_config():
    res, t = auto("sample_data_manufacturing")
    assert res["choices"]["master"] == "suppliers" and res["choices"]["entity_label"] == "Supplier"
    for key, text in [("S-102", "insurance expired"), ("S-102", "iso certification expired"), ("S-104", "possible duplicate"),
                      ("S-101", "possible duplicate entry"), ("S-102", "$27,500"), (None, "orion fabrication")]:
        assert has(t, key, text), (key, text)
    # "ACME CORP" (orders) and "ACME Corp." (certificates) must link to "Acme Corporation"
    assert not has(t, None, "acme")


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn(); print("ok", n)
