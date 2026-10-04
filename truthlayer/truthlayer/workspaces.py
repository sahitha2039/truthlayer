"""Workspaces: one per client company.

Each workspace is a folder:  workspaces/<slug>/
    meta.json      name, created date, which preset it started from
    config.yaml    the client's setup (written by the guided setup, or copied from a preset)
    data.db        that client's database (same fixed tables for every client)
    uploads/       every file exactly as it was uploaded
    staging/       files waiting in the guided setup

Nothing is shared between workspaces, so one client's data can never leak into another's.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

import yaml

from .engine import Engine
from .store import Store
from . import sqlviews

ROOT = Path(__file__).resolve().parent.parent
WSDIR = ROOT / "workspaces"

PRESETS = {
    "harborview": {"name": "Harborview Care Group", "industry": "Healthcare", "config": "clients/harborview.yaml", "sample": "sample_data",
                   "blurb": "Two nursing facilities: HR roster, payroll, license checks and a printed schedule (PDF). Uses the healthcare pack."},
    "healthcare": {"name": "Harborview Care Group", "industry": "Healthcare", "config": "clients/harborview.yaml", "sample": None, "empty": True,
                   "blurb": "The Harborview setup with no files loaded. Drop in the real HR, payroll, licensing and schedule files."},
    "northwind": {"name": "Northwind Outfitters", "industry": "Retail", "config": None, "sample": "sample_data_retail",
                  "blurb": "A retailer: CRM, billing and shipping. Set up automatically from the CSVs."},
    "apex": {"name": "Apex Manufacturing", "industry": "Manufacturing", "config": None, "sample": "sample_data_manufacturing",
             "blurb": "A manufacturer: suppliers, ISO certificates, purchase orders and invoices. Set up automatically from the CSVs."},
}

_lock = threading.RLock()
_cache: dict[str, "Workspace"] = {}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "company"


class Workspace:
    def __init__(self, slug: str):
        self.slug = slug
        self.dir = WSDIR / slug
        self.meta = json.loads((self.dir / "meta.json").read_text())
        self.store = Store(str(self.dir / "data.db"))
        self.reload()

    def reload(self):
        p = self.dir / "config.yaml"
        self.cfg = yaml.safe_load(p.read_text()) if p.exists() else None
        if self.cfg:
            self.cfg["client"] = self.meta["name"]
        self.engine = Engine(self.cfg, self.store) if self.cfg else None

    @property
    def configured(self):
        return self.engine is not None

    def save_config(self, cfg: dict):
        (self.dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
        self.reload()

    def config_text(self):
        p = self.dir / "config.yaml"
        return p.read_text() if p.exists() else ""

    # files are kept exactly as uploaded, so the setup can be redone later
    def keep_upload(self, filename: str, data: bytes) -> Path:
        d = self.dir / "uploads"
        d.mkdir(exist_ok=True)
        p = d / f"{datetime.now():%Y%m%d%H%M%S%f}_{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}"
        p.write_bytes(data)
        return p

    def staging(self) -> Path:
        d = self.dir / "staging"
        d.mkdir(exist_ok=True)
        return d

    def process(self):
        s = self.engine.process()
        sqlviews.build(self.store, self.cfg)
        return s

    def info(self):
        out = {"slug": self.slug, "name": self.meta["name"], "industry": self.meta.get("industry", ""), "created": self.meta.get("created"),
               "preset": self.meta.get("preset"), "configured": self.configured}
        if self.configured:
            s = self.engine.summary()
            out.update(entity_label=s["entity_label"], entities=s["entities"], records=s["records"],
                       urgent=s["by_severity"]["critical"], open_issues=s["open_issues"], sources=len(self.cfg["sources"]))
        return out


def list_all():
    WSDIR.mkdir(exist_ok=True)
    out = []
    for d in sorted(WSDIR.iterdir()):
        if (d / "meta.json").exists() and not d.name.startswith("_"):
            try:
                out.append(get(d.name).info())
            except Exception as e:  # a broken workspace shouldn't hide the others
                out.append({"slug": d.name, "name": d.name, "error": str(e), "configured": False})
    return out


def get(slug: str) -> Workspace:
    with _lock:
        if slug not in _cache:
            if not (WSDIR / slug / "meta.json").exists():
                raise KeyError(slug)
            _cache[slug] = Workspace(slug)
        return _cache[slug]


def create(name: str, industry: str = "", preset: str | None = None) -> Workspace:
    WSDIR.mkdir(exist_ok=True)
    base = slugify(name)
    slug, n = base, 2
    while (WSDIR / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    d = WSDIR / slug
    d.mkdir(parents=True)
    meta = {"name": name.strip() or "New company", "industry": industry, "created": datetime.now().isoformat(timespec="seconds"), "preset": preset}
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    ws = get(slug)
    ws.store.set_setting("as_of", datetime.now().date().isoformat())
    ws.store.event("workspace", f"Workspace created for {meta['name']}" + (f" from the {preset} preset" if preset else ""))
    return ws


def remove(slug: str):
    with _lock:
        _cache.pop(slug, None)
        src = WSDIR / slug
        if src.exists():
            dest = WSDIR / f"_deleted_{slug}_{datetime.now():%Y%m%d%H%M%S}"
            shutil.move(str(src), str(dest))   # kept on disk, just hidden


def rename(slug: str, name: str):
    ws = get(slug)
    ws.meta["name"] = name
    (ws.dir / "meta.json").write_text(json.dumps(ws.meta, indent=1))
    ws.reload()
