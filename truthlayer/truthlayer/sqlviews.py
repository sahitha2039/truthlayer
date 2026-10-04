"""Read-only SQL views, one per uploaded system, so each client's data can be queried like
normal tables ("SELECT * FROM file_payroll") without ever creating tables from user input.

The underlying storage stays a fixed schema. The views are rebuilt after each processing run,
and every name that goes into SQL is checked against a strict pattern first.
"""
import re

SAFE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _q(name):
    if not SAFE.match(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return f'"{name}"'


def build(store, cfg):
    with store.lock:
        db = store.db
        for (v,) in db.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall():
            if v.startswith(("file_", "profiles")):
                db.execute(f'DROP VIEW IF EXISTS "{v}"')
        # the shape of each client's data, stored as rows
        db.execute("CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, label TEXT, format TEXT, view TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS source_fields (source TEXT, field TEXT, type TEXT, original_headers TEXT)")
        db.execute("DELETE FROM sources")
        db.execute("DELETE FROM source_fields")
        for sid, s in cfg["sources"].items():
            if not SAFE.match(sid):
                continue
            fields = [f for f in s["fields"] if SAFE.match(f)]
            view = f"file_{sid}"
            db.execute("INSERT INTO sources VALUES (?,?,?,?)", (sid, s.get("label", sid), s.get("format", "csv"), view))
            for f in fields:
                db.execute("INSERT INTO source_fields VALUES (?,?,?,?)", (sid, f, s["fields"][f].get("type", "text"), ", ".join(s["fields"][f].get("synonyms", []))))
            cols = ", ".join(f"json_extract(r.raw_json, '$.\"{f}\"') AS {_q(f)}" for f in fields)
            db.execute(f"""CREATE VIEW {_q(view)} AS SELECT r.id AS record_id, f.filename AS file, r.locator AS location,
                           r.entity_key AS linked_to{', ' + cols if cols else ''}
                           FROM records r JOIN files f ON f.id = r.file_id WHERE f.active = 1 AND r.source = '{sid}'""")
        attrs = [a for a in cfg["attributes"] if SAFE.match(a) and a not in ("name", "key", "status", "open_issues")]
        cols = ", ".join(f"json_extract(attrs_json, '$.\"{a}\".value') AS {_q(a)}" for a in attrs)
        db.execute(f"""CREATE VIEW profiles AS SELECT key, display_name AS name, status, open_issues{', ' + cols if cols else ''}
                       FROM entities""")
        db.commit()


def list_tables(store):
    out = []
    for (v,) in store.db.execute("SELECT name FROM sqlite_master WHERE type='view' AND (name LIKE 'file_%' OR name='profiles') ORDER BY name").fetchall():
        cur = store.db.execute(f'SELECT * FROM "{v}" LIMIT 0')
        n = store.db.execute(f'SELECT COUNT(*) FROM "{v}"').fetchone()[0]
        label = store.db.execute("SELECT label FROM sources WHERE view=?", (v,)).fetchone()
        out.append({"name": v, "label": label[0] if label else "Combined profiles", "columns": [d[0] for d in cur.description], "rows": n})
    return out


def rows(store, name, limit=200):
    if name not in {t["name"] for t in list_tables(store)}:
        raise KeyError(name)
    cur = store.db.execute(f'SELECT * FROM "{name}" LIMIT ?', (min(int(limit), 1000),))
    return {"columns": [d[0] for d in cur.description], "rows": [list(r) for r in cur.fetchall()]}
