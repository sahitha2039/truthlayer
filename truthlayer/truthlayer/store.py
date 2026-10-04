"""Persistence. Plain SQL on SQLite (zero setup for a live demo); the schema is
portable to PostgreSQL unchanged apart from the connection.

Layers:
  files, records          raw evidence exactly as received (never modified)
  entities, evidence      derived: canonical view + which record supports which value
  issues                  derived: everything that needs a human, keyed by a stable fingerprint
  decisions, match_overrides, events   human input + audit log (survive re-processing)
"""
import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY, source TEXT, filename TEXT, sha256 TEXT, uploaded_at TEXT,
  active INTEGER DEFAULT 1, row_count INTEGER, notes_json TEXT);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY, file_id INTEGER, source TEXT, row_num INTEGER, locator TEXT,
  raw_json TEXT, norm_json TEXT, problems_json TEXT,
  entity_key TEXT, match_method TEXT, match_score REAL, match_status TEXT);
CREATE TABLE IF NOT EXISTS entities (
  key TEXT PRIMARY KEY, type TEXT, display_name TEXT, status TEXT,
  attrs_json TEXT, sources_json TEXT, open_issues INTEGER DEFAULT 0, worst TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, entity_key TEXT, attribute TEXT, source TEXT, record_id INTEGER,
  raw_value TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, rule TEXT, severity TEXT, category TEXT,
  entity_key TEXT, attribute TEXT, title TEXT, detail TEXT, action TEXT, data_json TEXT,
  status TEXT, first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, fingerprint TEXT, decision TEXT, value TEXT, note TEXT,
  reviewer TEXT, decided_at TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS match_overrides (
  id INTEGER PRIMARY KEY, source TEXT, name_key TEXT, entity_key TEXT, reviewer TEXT, decided_at TEXT,
  UNIQUE(source, name_key));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT, kind TEXT, entity_key TEXT, fingerprint TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_rec_entity ON records(entity_key);
CREATE INDEX IF NOT EXISTS ix_ev_entity ON evidence(entity_key);
CREATE INDEX IF NOT EXISTS ix_issue_entity ON issues(entity_key);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def many(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    def event(self, kind, message, entity_key=None, fingerprint=None):
        self.x("INSERT INTO events(at, kind, entity_key, fingerprint, message) VALUES (?,?,?,?,?)",
               (now(), kind, entity_key, fingerprint, message))

    def setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.x("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def reset(self):
        with self.lock:
            for t in ("files", "records", "entities", "evidence", "issues", "decisions", "match_overrides", "events"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.commit()


def dumps(o):
    return json.dumps(o, default=str)
