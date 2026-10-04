"""Ingestion: bytes in -> (source name, list of raw row dicts with provenance, notes).

* Tabular files (CSV/TSV/XLSX) are matched to a configured source by their headers,
  using field names + synonyms + fuzzy matching, so renamed or reordered columns
  still load. Unmapped columns are kept in the raw record and reported.
* PDFs go through a named connector (e.g. `weekly_grid`) declared in the config.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from difflib import SequenceMatcher

from .normalize import simplify, is_empty


# ---------------------------------------------------------------- tabular
def read_table(data: bytes, filename: str) -> list[list[str]]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else (v.date().isoformat() if hasattr(v, "date") and callable(v.date) else str(v)) for v in r])
        return rows
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def _field_aliases(name: str, spec: dict) -> list[str]:
    return [simplify(name)] + [simplify(s) for s in spec.get("synonyms", [])]


def map_headers(headers: list[str], fields: dict) -> tuple[dict, list[str], float]:
    """Return ({field: column index}, unmapped header names, score 0..1)."""
    mapping, used = {}, set()
    hs = [simplify(h) for h in headers]
    # pass 1 exact alias, pass 2 fuzzy
    for exact in (True, False):
        for fname, spec in fields.items():
            if fname in mapping:
                continue
            aliases = _field_aliases(fname, spec)
            best, bscore = None, 0.0
            for i, h in enumerate(hs):
                if i in used or not h:
                    continue
                if exact:
                    s = 1.0 if h in aliases else 0.0
                else:
                    s = max(SequenceMatcher(None, h, a).ratio() for a in aliases)
                if s > bscore:
                    best, bscore = i, s
            if best is not None and (bscore == 1.0 if exact else bscore >= 0.82):
                mapping[fname] = best
                used.add(best)
    unmapped = [headers[i] for i in range(len(headers)) if i not in used and headers[i]]
    required = [f for f, s in fields.items() if s.get("required")]
    score = len(mapping) / max(1, len(fields))
    if all(r in mapping for r in required):
        score += 1
    return mapping, unmapped, score


def detect_tabular(rows: list[list[str]], filename: str, cfg: dict, forced: str | None):
    """Find the header row and the best-matching source."""
    candidates = {k: v for k, v in cfg["sources"].items() if v.get("format", "csv") != "pdf"}
    if forced:
        candidates = {forced: cfg["sources"][forced]}
    best = None
    for hi, row in enumerate(rows[:15]):
        if sum(1 for c in row if c) < 2:
            continue
        for name, src in candidates.items():
            mapping, unmapped, score = map_headers(row, src["fields"])
            fn = simplify(filename)
            if name in fn or simplify(src.get("label", "")) in fn:
                score += 0.25
            if best is None or score > best[0]:
                best = (score, name, hi, mapping, unmapped)
    if best is None or best[0] < 0.5:
        raise ValueError("Could not recognise this file's columns as any configured source.")
    return best[1:]


def ingest_tabular(data: bytes, filename: str, cfg: dict, forced: str | None = None):
    rows = read_table(data, filename)
    source, hi, mapping, unmapped = detect_tabular(rows, filename, cfg, forced)
    headers = rows[hi]
    fields = cfg["sources"][source]["fields"]
    out = []
    for n, row in enumerate(rows[hi + 1:], start=hi + 2):
        if not any(c.strip() for c in row):
            continue
        raw = {f: (row[i] if i < len(row) else "") for f, i in mapping.items()}
        extra = {headers[i]: row[i] for i in range(min(len(headers), len(row))) if i not in mapping.values() and headers[i]}
        if extra:
            raw["_extra"] = extra
        if len(row) > len(headers) and any(row[len(headers):]):
            raw["_overflow"] = row[len(headers):]
        out.append({"row_num": n, "locator": f"row {n}", "raw": raw})
    notes = {
        "header_row": hi + 1,
        "mapping": {f: headers[i] for f, i in mapping.items()},
        "missing_fields": [f for f in fields if f not in mapping],
        "unmapped_columns": unmapped,
    }
    return source, out, notes


# ---------------------------------------------------------------- PDF: weekly grid
DAY_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s*(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?", re.I)
TIME_RE = re.compile(r"^(\d{1,2})(?::?(\d{2}))?\s*(a|p|am|pm)?\s*-\s*(\d{1,2})(?::?(\d{2}))?\s*(a|p|am|pm)?$")
OFF_CODES = {"off", "", "-", "x", "--", "n/a"}
LEAVE_CODES = {"pto", "vac", "vacation", "sick", "loa", "leave", "hol", "holiday", "fmla", "bereavement", "jury"}


def _clean_shift(s: str) -> str:
    s = (s or "").lower().replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+to\s+", "-", s)
    return re.sub(r"\s+", "", s)


def shift_hours(code: str, legend: dict):
    """-> (hours, start_hour, problem). start_hour in 0..24 (for coverage bands)."""
    c = _clean_shift(code)
    m = TIME_RE.match(c)
    start = None
    computed = None
    if m:
        h1, m1, ap1, h2, m2, ap2 = m.groups()

        def to24(h, mi, ap, other_ap):
            h = int(h) % 24
            ap = ap or other_ap
            if ap and ap.startswith("p") and h < 12:
                h += 12
            if ap and ap.startswith("a") and h == 12:
                h = 0
            return h + int(mi or 0) / 60

        t1, t2 = to24(h1, m1, ap1, None), to24(h2, m2, ap2, None)
        if not ap1 and not ap2 and t1 <= 12 and t2 <= 12 and t2 <= t1:
            t2 += 12  # e.g. "7-3" -> 7am-3pm
        start = t1
        computed = (t2 - t1) % 24 or 24
    if c in legend:
        return legend[c], start, None
    if computed is not None:
        problem = f"shift '{code}' is not in the printed shift legend; hours computed from times" if legend else None
        return round(computed, 2), start, problem
    return None, None, f"unrecognised schedule entry '{code}'"


def parse_legend(text: str) -> dict:
    legend = {}
    for m in re.finditer(r"((?:[0-9]{1,2}(?::\d{2})?\s*[ap]?m?\s*[-–]\s*[0-9]{1,2}(?::\d{2})?\s*[ap]?m?[\s,]*(?:and|&)?\s*)+)\s*(?:are|is|=|:)\s*(\d+(?:\.\d+)?)\s*h", text, re.I):
        hours = float(m.group(2))
        for tok in re.findall(r"[0-9]{1,2}(?::\d{2})?\s*[ap]?m?\s*[-–]\s*[0-9]{1,2}(?::\d{2})?\s*[ap]?m?", m.group(1), re.I):
            legend[_clean_shift(tok)] = int(hours) if hours.is_integer() else hours
    return legend


def _infer_year(month: int, day: int, explicit, as_of: date) -> int:
    if explicit:
        y = int(explicit)
        return y + 2000 if y < 100 else y
    y = as_of.year
    try:
        if date(y, month, day) - as_of > timedelta(days=183):
            y -= 1
    except ValueError:
        pass
    return y


def _rows_from_text(text: str, legend: dict) -> list[list[str]]:
    """Fallback when no table lines are detectable: split each line into tokens."""
    rows = []
    cell = r"(OFF|PTO|VAC|SICK|LOA|[0-9]{1,2}(?::\d{2})?[ap]?m?\s*[-–]\s*[0-9]{1,2}(?::\d{2})?[ap]?m?)"
    for line in text.splitlines():
        if DAY_RE.search(line) and len(DAY_RE.findall(line)) >= 3:
            rows.append(["Staff", "Role"] + [" ".join(g for g in m.groups()[:1]) + f" {m.group(2)}/{m.group(3)}" for m in DAY_RE.finditer(line)])
            continue
        cells = re.findall(cell, line, re.I)
        if len(cells) >= 5:
            first = re.search(cell, line, re.I).start()
            head = line[:first].split()
            if len(head) >= 2:
                rows.append([" ".join(head[:-1]), head[-1]] + cells)
    return rows


def ingest_weekly_grid(data: bytes, filename: str, cfg: dict, as_of: date, source: str):
    import pdfplumber
    fac_ref = None
    for spec in cfg["sources"][source]["fields"].values():
        if spec.get("type") == "code" and spec.get("ref") == "facility":
            from .normalize import build_ref
            fac_ref = build_ref(cfg["reference"]["facility"])
    out, notes = [], {"pages": []}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            legend = parse_legend(text)
            facility_raw = None
            if fac_ref:
                for line in text.splitlines()[:6] + text.splitlines():
                    s = simplify(line)
                    for alias in sorted(fac_ref, key=len, reverse=True):
                        if len(alias) >= 4 and re.search(rf"\b{re.escape(alias)}\b", s):
                            facility_raw = alias
                            break
                    if facility_raw:
                        break
            tables = page.extract_tables() or []
            if not tables:
                tables = page.extract_tables({"vertical_strategy": "text", "horizontal_strategy": "text"}) or []
            rows = [r for t in tables for r in t]
            if not any(len(DAY_RE.findall(" ".join(c or "" for c in r))) >= 3 for r in rows):
                rows = _rows_from_text(text, legend)
            header, day_cols, name_col, role_col = None, {}, 0, None
            page_rows = 0
            for ri, r in enumerate(rows):
                cells = [re.sub(r"\s+", " ", c or "").strip() for c in r]
                if sum(1 for c in cells if DAY_RE.search(c)) >= 3:
                    header = cells
                    day_cols = {}
                    for i, c in enumerate(cells):
                        m = DAY_RE.search(c)
                        if m:
                            mo, dy = int(m.group(2)), int(m.group(3))
                            y = _infer_year(mo, dy, m.group(4), as_of)
                            try:
                                day_cols[i] = date(y, mo, dy).isoformat()
                            except ValueError:
                                pass
                    for i, c in enumerate(cells):
                        sc = simplify(c)
                        if sc in ("staff", "name", "employee", "staff name", "employee name"):
                            name_col = i
                        if sc in ("role", "title", "position", "job", "discipline"):
                            role_col = i
                    if role_col is None and len(cells) > 1 and 1 not in day_cols:
                        role_col = 1
                    continue
                if not header or not cells or name_col >= len(cells) or not cells[name_col]:
                    continue
                if "shift" in cells[name_col].lower() and ":" in cells[name_col]:
                    continue
                page_rows += 1
                for ci, d in day_cols.items():
                    code = cells[ci] if ci < len(cells) else ""
                    out.append({
                        "row_num": pno * 1000 + ri,
                        "locator": f"page {pno}, row {ri}, {header[ci]}",
                        "raw": {
                            "staff_name": cells[name_col],
                            "role": cells[role_col] if role_col is not None and role_col < len(cells) else "",
                            "facility": facility_raw or "",
                            "date": d,
                            "shift": code,
                            "_legend": legend,
                        },
                    })
            notes["pages"].append({"page": pno, "facility": facility_raw, "staff_rows": page_rows,
                                   "days": len(day_cols), "legend": legend})
    if not out:
        raise ValueError("No schedule grid found in this PDF.")
    return out, notes


def ingest_file(data: bytes, filename: str, cfg: dict, as_of: date, forced: str | None = None):
    is_pdf = filename.lower().endswith(".pdf") or data[:4] == b"%PDF"
    if is_pdf:
        pdf_sources = [k for k, v in cfg["sources"].items() if v.get("format") == "pdf"]
        source = forced or (pdf_sources[0] if pdf_sources else None)
        if not source:
            raise ValueError("No PDF source is configured for this client.")
        parser = cfg["sources"][source].get("parser")
        if parser != "weekly_grid":
            raise ValueError(f"Unknown PDF parser '{parser}'")
        rows, notes = ingest_weekly_grid(data, filename, cfg, as_of, source)
        return source, rows, notes
    return ingest_tabular(data, filename, cfg, forced)
