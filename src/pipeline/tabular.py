"""Read book lists from CSV or Excel (.xlsx) with automatic header-row detection.

Excel exports often have a title row above the header and a second (translated) header row
below it, e.g. row 1 "Last updated", row 2 English headers, row 3 German headers. We pick the
first row that contains a recognised ID column, and skip rows that repeat a header label.
Only the columns the pipeline needs are returned; all other business data is ignored.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterator

from .util import read_csv_rows

# canonical field -> accepted header names (lower-case, English + German)
FIELDS: dict[str, tuple[str, ...]] = {
    "isbn": ("isbn13", "isbn-13", "isbn_13", "isbn", "ean", "ean13", "gtin"),
    "asin": ("asin",),
    "title": ("title", "titel", "book_title", "buchtitel"),
    "publisher": ("publisher", "verlag", "brand", "marke", "imprint"),
    "author": ("author", "autor", "autorin", "contributor"),
    "publication_date": ("publication date", "publication_date", "veröffentlichungsdatum", "erscheinungsdatum"),
    "source_ref": ("source_ref", "vlbtix_url", "vlb_url", "source_url", "url"),
    # only used to draw a balanced discovery sample (sample-books), never stored as a feature
    "sales_band": ("type of release (sales rank)", "sales_band", "sales band"),
    "price": ("amz offer", "price", "price_eur", "preis", "angebotspreis"),
}
ID_FIELDS = ("isbn", "asin")
HEADER_SCAN_ROWS = 15


def _norm_header(v) -> str:
    return " ".join(str(v).strip().lower().split()) if v is not None else ""


def _cell_str(v) -> str:
    """Excel cells may be int/float/date; turn them into clean strings without '.0' or exponent."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.date().isoformat() if isinstance(v, dt.datetime) else v.isoformat()
    return str(v).strip()


def _map_columns(header_row: list) -> dict[str, int]:
    names = [_norm_header(h) for h in header_row]
    mapping: dict[str, int] = {}
    for field, aliases in FIELDS.items():
        for alias in aliases:  # alias order = priority (e.g. isbn13 before ean)
            if alias in names:
                mapping[field] = names.index(alias)
                break
    return mapping


def _header_labels() -> set[str]:
    return {a for aliases in FIELDS.values() for a in aliases}


def read_book_rows(path: Path, sheet: str | None = None) -> Iterator[tuple[int, dict[str, str]]]:
    """Yield (spreadsheet_line_number, {field: value}) for every data row."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        yield from _read_xlsx(path, sheet)
        return
    labels = _header_labels()
    for line_no, row in enumerate(read_csv_rows(path), start=2):
        rec = {}
        for field, aliases in FIELDS.items():
            for a in aliases:
                if row.get(a):
                    rec[field] = row[a]
                    break
        if _norm_header(rec.get("isbn")) in labels or _norm_header(rec.get("asin")) in labels:
            continue  # repeated header line
        yield line_no, rec


def _read_xlsx(path: Path, sheet: str | None) -> Iterator[tuple[int, dict[str, str]]]:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("Reading .xlsx needs openpyxl: python -m pip install openpyxl") from None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)  # values only, no macros/formulas run
    try:
        if sheet:
            if sheet not in wb.sheetnames:
                raise ValueError(f"sheet {sheet!r} not found; available: {wb.sheetnames}")
            ws = wb[sheet]
        else:
            ws = wb.worksheets[0]
        mapping: dict[str, int] | None = None
        labels = _header_labels()
        for line_no, row in enumerate(ws.iter_rows(values_only=True), start=1):
            row = list(row)
            if mapping is None:
                if line_no > HEADER_SCAN_ROWS:
                    raise ValueError(f"no ISBN/EAN/ASIN header found in the first {HEADER_SCAN_ROWS} rows "
                                     f"of sheet {ws.title!r}")
                m = _map_columns(row)
                if any(f in m for f in ID_FIELDS):
                    mapping = m
                continue
            if not any(c not in (None, "") for c in row):
                continue  # empty row
            rec = {f: _cell_str(row[i]) if i < len(row) else "" for f, i in mapping.items()}
            if _norm_header(rec.get("isbn")) in labels or _norm_header(rec.get("asin")) in labels:
                continue  # second (e.g. German) header row
            yield line_no, rec
    finally:
        wb.close()


def list_sheets(path: Path) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return wb.sheetnames
    finally:
        wb.close()
