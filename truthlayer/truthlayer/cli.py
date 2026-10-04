"""Command-line fallback: ingest files and print every flag. Useful if the UI can't be shown.

    python -m truthlayer.cli sample_data/*            # Harborview
    python -m truthlayer.cli --config clients/x.yaml files...
"""
import argparse
import os
import tempfile

import yaml

from .engine import Engine
from .store import Store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "clients", "harborview.yaml"))
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--db", default=None, help="SQLite file (default: temporary)")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    db = a.db or os.path.join(tempfile.mkdtemp(), "cli.db")
    st = Store(db)
    if a.as_of:
        st.set_setting("as_of", a.as_of)
    eng = Engine(cfg, st)
    for f in a.files:
        try:
            r = eng.ingest(open(f, "rb").read(), os.path.basename(f))
            n = r["notes"]
            print(f"✓ {os.path.basename(f):28} → {r['label']:20} {r['records']:4} records"
                  + (f"  missing fields: {n['missing_fields']}" if n.get("missing_fields") else "")
                  + (f"  unmapped: {n['unmapped_columns']}" if n.get("unmapped_columns") else ""))
        except Exception as e:
            print(f"✗ {f}: {e}")
    s = eng.process()
    m = s["metrics"]
    print(f"\n{s['entities']} entities (+{s['unverified_entities']} unverified) · {s['records']} records · "
          f"{m['records_linked']}% linked · {m['fields_agreeing']}% fields agree · {s['open_issues']} open issues "
          f"({s['by_severity']['critical']} critical, {s['by_severity']['warning']} warning, {s['by_severity']['info']} info)\n")
    for i in st.q("""SELECT i.severity, i.category, i.entity_key, e.display_name, i.title, i.detail FROM issues i
                     LEFT JOIN entities e ON e.key=i.entity_key WHERE i.active=1
                     ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, i.category"""):
        who = f"{i['display_name'] or ''} ({i['entity_key']})" if i["entity_key"] else ""
        print(f"[{i['severity'].upper():8}] {i['category']:12} {who:32} {i['title']}")
        print(f"{'':12}{i['detail'][:160]}")


if __name__ == "__main__":
    main()
