"""Shared helpers: CSV I/O (Excel/German-locale friendly), logging, JSON."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger("pipeline")

# German/Austrian Excel separates columns with ';' - with ',' everything lands in column A.
CSV_DELIMITER = ";"

_SECRET_PAT = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|cookie)(\s*[=:]\s*)(\S+)")


class RedactSecrets(logging.Filter):
    """Masks anything that looks like key=value secrets before it reaches a log sink."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = _SECRET_PAT.sub(r"\1\2***", msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("pipeline")
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.FileHandler(log_dir / "pipeline.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    for h in (fh, ch):
        h.addFilter(RedactSecrets())
        root.addHandler(h)


def read_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    """Stream rows from a CSV. Handles UTF-8 (with/without BOM), cp1252 and ',' or ';' delimiters
    (German Excel saves with ';'). Header names are lower-cased and stripped."""
    raw = path.read_bytes()[:65536]
    encoding = "utf-8-sig"
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        encoding = "cp1252"
    sample = raw.decode(encoding, errors="replace")
    first = sample.splitlines()[0] if sample else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    with path.open("r", encoding=encoding, newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delim)
        if reader.fieldnames:
            reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
        for row in reader:
            yield {k: (v or "").strip() for k, v in row.items() if k}


def csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection (values from images/ChatGPT end up in Excel)."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _replace_or_fallback(tmp: Path, path: Path) -> Path:
    """Move tmp into place. If Windows refuses because the file is open (e.g. in Excel), save under a
    timestamped name instead of failing, and say so."""
    try:
        os.replace(tmp, path)
        return path
    except PermissionError:
        import datetime as _dt
        alt = path.with_name(f"{path.stem}_{_dt.datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        os.replace(tmp, alt)
        log.warning("%s is open in another program (Excel?) - saved as %s instead. "
                    "Close the file to have it updated next time.", path.name, alt.name)
        return alt


def write_csv(path: Path, header: list[str], rows: Iterable[Iterable]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:  # BOM so Excel shows umlauts
        w = csv.writer(fh, delimiter=CSV_DELIMITER)
        w.writerow(header)
        for r in rows:
            w.writerow([csv_safe(v) for v in r])
            n += 1
    _replace_or_fallback(tmp, path)
    return n


def write_xlsx(path: Path, sheets: list[tuple[str, list[str], list]], col_widths: dict[str, int] | None = None) -> Path:
    """Write a formatted Excel file: one tab per (name, header, rows); bold frozen header, filter,
    sensible column widths, clickable links. Text that starts with '=' stays text (no formulas)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    fill = PatternFill("solid", fgColor="DDE5F0")
    for name, header, rows in sheets:
        ws = wb.create_sheet(name[:31])
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = fill
            c.alignment = Alignment(vertical="center")
        widths = [len(h) for h in header]
        row_idx = 1                                    # tracked here: ws.max_row is slow on big sheets
        for r in rows:
            ws.append([None if v is None else v for v in r])
            row_idx += 1
            for j, v in enumerate(r):
                if isinstance(v, str):
                    if v.startswith(("=", "+", "-", "@")):
                        ws.cell(row=row_idx, column=j + 1).data_type = "s"   # never evaluate as formula
                    elif v.startswith(("http://", "https://")):
                        cell = ws.cell(row=row_idx, column=j + 1)
                        cell.hyperlink = v
                        cell.style = "Hyperlink"
                    widths[j] = max(widths[j], min(len(v), 60))
                elif v is not None:
                    widths[j] = max(widths[j], len(str(v)))
        for j, h in enumerate(header):
            w = (col_widths or {}).get(h, widths[j] + 2)
            ws.column_dimensions[get_column_letter(j + 1)].width = max(8, min(w, 62))
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    return _replace_or_fallback(tmp, path)


def read_table(path: Path) -> list[dict[str, str]]:
    """Rows of a CSV or .xlsx (first sheet, first row = header); header names lower-cased."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            it = wb.worksheets[0].iter_rows(values_only=True)
            header = [str(h or "").strip().lower() for h in next(it, [])]
            out = []
            for row in it:
                if not any(v not in (None, "") for v in row):
                    continue
                out.append({h: ("" if v is None else str(v).strip()) for h, v in zip(header, row) if h})
            return out
        finally:
            wb.close()
    return list(read_csv_rows(path))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _replace_or_fallback(tmp, path)


def strip_code_fences(text: str) -> str:
    """ChatGPT often wraps JSON in ```json fences; remove them."""
    """...and often adds a sentence before/after; extract the JSON part."""
    t = text.strip()
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)\n?```", t, re.S)
    if m:
        return m.group(1).strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if starts and min(starts) > 0:
        start = min(starts)
        end = max(t.rfind("}"), t.rfind("]"))
        if end > start:
            return t[start:end + 1]
    return t


def slug(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:max_len] or "unnamed"


def _stringio_csv(header, rows) -> str:  # used in tests
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()
