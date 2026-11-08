"""Excel as a view over the record store.

Export: one formatted sheet per collection (Excel Table, enum data-validation, conditional formatting, frozen
header), an Activity sheet from record_events and a Stats sheet with live formulas + chart. Hidden `_row_id`
and `_row_hash` columns make rows traceable. Written to a temp file, atomically swapped in, then re-opened
and verified. The previous file is copied to backups/ first.

Import (three-way): for every row, compare the Excel row hash with the hash recorded at export time (did the
user edit it?) and the DB version with the version at export time (did Forge edit it?). Only-Excel edits are
validated and applied; both-sides edits become record_conflicts for the user to resolve in the TUI.

openpyxl limitation: loading and re-saving an *arbitrary* workbook drops charts/images/pivots. Exports are
regenerated from the DB so this does not apply to them; `write_cells` refuses such workbooks and uses COM.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo

from forge.config import forge_home
from forge.db import canonical, now, sha
from forge.errors import InvalidInput, ResourceUnavailable
from forge.records.store import CollectionSpec, RecordStore

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
STATUS_COLORS = {"Offer": "C6EFCE", "Interview": "FFEB9C", "Rejected": "FFC7CE", "Submitted": "DDEBF7", "Withdrawn": "EDEDED"}


def create_workbook(path: Path, sheet: str, headers: list[str], rows: list[list[Any]]) -> dict:
    """Create a new workbook independently of installed desktop applications."""
    path = path.expanduser()
    if path.suffix.lower() != ".xlsx":
        raise InvalidInput("excel.create requires an .xlsx path")
    if path.exists():
        raise InvalidInput("Workbook already exists; use excel.write_cells to edit it or choose a new filename")
    if not sheet or len(sheet) > 31 or any(c in sheet for c in r"\/*?:[]"):
        raise InvalidInput("invalid Excel sheet name")
    if not headers or len(headers) > 16384 or len(rows) > 1048575:
        raise InvalidInput("headers and rows must fit within Excel's worksheet limits")
    if any(len(row) != len(headers) for row in rows):
        raise InvalidInput("each row must have the same number of cells as headers")
    if any(not isinstance(v, (str, int, float, bool, type(None))) for row in rows for v in row):
        raise InvalidInput("cells must contain text, numbers, booleans, or null")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    # Scraped text is literal data, including strings beginning with '='.
    for r, values in enumerate([headers, *rows], 1):
        for c, value in enumerate(values, 1):
            cell = ws.cell(r, c, value)
            if isinstance(value, str):
                cell.data_type = "s"
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(60, max(14, max(len(str(c.value or "")) for c in col) + 2))
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as f:
        temp = Path(f.name)
    created = False
    try:
        wb.save(temp)
        check = load_workbook(temp, read_only=True, data_only=False)
        try:
            actual = list(check[sheet].iter_rows(values_only=True))
            expected = [tuple(_norm_val(v) for v in row) for row in [headers, *rows]]
            if actual != expected:
                raise InvalidInput("workbook read-back verification failed")
        finally:
            check.close()
        # Exclusive creation protects an existing workbook, including one created during verification.
        with path.open("xb") as dest:
            created = True
            with temp.open("rb") as source:
                shutil.copyfileobj(source, dest)
    except FileExistsError:
        raise InvalidInput("Workbook already exists; choose a new filename") from None
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise
    finally:
        wb.close()
        temp.unlink(missing_ok=True)
    return {"path": str(path), "sheet": sheet, "rows": len(rows), "columns": len(headers), "verified": True}


def lock_file(path: Path) -> Path:
    return path.with_name("~$" + path.name)


def ensure_not_open(path: Path) -> None:
    if lock_file(path).exists():
        raise ResourceUnavailable(f"{path.name} is open in Excel (lock file present); close it or retry later")
    if path.exists():
        try:
            with open(path, "r+b"):
                pass
        except PermissionError:
            raise ResourceUnavailable(f"{path.name} is locked by another process") from None


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    bdir = forge_home() / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"{path.stem}.{dt.datetime.now():%Y%m%d-%H%M%S}{path.suffix}"
    shutil.copy2(path, dest)
    return dest


def row_hash(spec: CollectionSpec, rec: dict) -> str:
    return sha({f.name: _norm_val(rec.get(f.name)) for f in spec.fields})[:16]


def _norm_val(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time() else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if v == "":
        return None
    return v


def _to_cell(fspec, v):
    if v is None:
        return None
    if fspec.type == "date":
        try:
            return dt.date.fromisoformat(str(v)[:10])
        except ValueError:
            return v
    return v


def export_collections(store: RecordStore, collections: list[str], path: Path, *, stats: bool = True) -> dict:
    path = Path(path).expanduser()
    ensure_not_open(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bk = backup(path)
    wb = Workbook()
    wb.remove(wb.active)
    summary: dict[str, Any] = {"path": str(path), "backup": str(bk) if bk else None, "sheets": {}}
    hashes: dict[str, dict] = {}
    for cname in collections:
        spec = store.spec(cname)
        recs = store.list(cname)
        ws = wb.create_sheet(spec.title[:31])
        cols = ["ID"] + [f.name for f in spec.fields] + ["_row_hash"]
        ws.append(cols)
        h: dict[str, str] = {}
        vers: dict[str, int] = {}
        for r in recs:
            rh = row_hash(spec, r)
            h[r["id"]] = rh
            vers[r["id"]] = r["_version"]
            ws.append([r["id"]] + [_to_cell(f, r.get(f.name)) for f in spec.fields] + [rh])
        hashes[cname] = {"hashes": h, "versions": vers}
        n = len(recs)
        last_col = get_column_letter(len(cols))
        for i, c in enumerate(ws[1], start=1):
            c.fill, c.font, c.alignment = HEADER_FILL, HEADER_FONT, Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions["A"].width = 16
        for i, f in enumerate(spec.fields, start=2):
            letter = get_column_letter(i)
            ws.column_dimensions[letter].width = f.width
            if f.type == "date":
                for row in ws.iter_rows(min_row=2, max_row=n + 1, min_col=i, max_col=i):
                    row[0].number_format = "yyyy-mm-dd"
            if f.type in ("text",):
                for row in ws.iter_rows(min_row=2, max_row=n + 1, min_col=i, max_col=i):
                    row[0].alignment = Alignment(wrap_text=True, vertical="top")
            rng = f"{letter}2:{letter}{max(n + 200, 500)}"
            if f.type == "enum" and f.enum:
                formula = '"' + ",".join(f.enum) + '"'
                if len(formula) <= 255:
                    dv = DataValidation(type="list", formula1=formula, allow_blank=not f.required, showErrorMessage=True,
                                        errorTitle="Invalid value", error=f"Choose one of: {', '.join(f.enum)}")
                    ws.add_data_validation(dv)
                    dv.add(rng)
            elif f.type in ("integer", "number") and (f.min is not None or f.max is not None):
                dv = DataValidation(type="decimal", operator="between", formula1=str(f.min if f.min is not None else -1e12),
                                    formula2=str(f.max if f.max is not None else 1e12), allow_blank=True, showErrorMessage=True)
                ws.add_data_validation(dv)
                dv.add(rng)
                if f.min is not None and f.max is not None and n:
                    ws.conditional_formatting.add(f"{letter}2:{letter}{n + 1}", ColorScaleRule(start_type="num", start_value=f.min, start_color="F8696B",
                                                                                         mid_type="percentile", mid_value=50, mid_color="FFEB84",
                                                                                         end_type="num", end_value=f.max, end_color="63BE7B"))
            elif f.type == "date" and n:
                dv = DataValidation(type="date", operator="greaterThan", formula1="1", allow_blank=True)
                ws.add_data_validation(dv)
                dv.add(rng)
            if spec.status_field == f.name and n:
                for status, color in STATUS_COLORS.items():
                    ws.conditional_formatting.add(f"A2:{last_col}{n + 1}", FormulaRule(formula=[f'${letter}2="{status}"'],
                                                                                     fill=PatternFill("solid", fgColor=color)))
        ws.column_dimensions[last_col].hidden = True
        ws.freeze_panes = "B2"
        if n:
            tname = "".join(ch for ch in spec.title.title() if ch.isalnum())[:30] or "T"
            t = Table(displayName=f"tbl{tname}", ref=f"A1:{last_col}{n + 1}")
            t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            ws.add_table(t)
        else:
            ws.auto_filter.ref = f"A1:{last_col}1"
        summary["sheets"][spec.title] = n

    # Activity log
    act = wb.create_sheet("Activity Log")
    act.append(["Timestamp", "Collection", "Record", "Actor", "Field", "Old", "New"])
    for c in act[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
    for cname in collections:
        for e in store.history(cname, limit=2000):
            act.append([dt.datetime.fromtimestamp(e["ts"]), cname, e["record_id"], e["actor"], e["field"], e["old_value"], e["new_value"]])
    for col, w in zip("ABCDEFG", (20, 16, 16, 18, 18, 30, 30)):
        act.column_dimensions[col].width = w
    for row in act.iter_rows(min_row=2, min_col=1, max_col=1):
        row[0].number_format = "yyyy-mm-dd hh:mm"
    act.freeze_panes = "A2"

    if stats:
        _stats_sheet(wb, store, collections)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=str(path.parent))
    os.close(fd)
    try:
        wb.save(tmp)
        verify = load_workbook(tmp, read_only=True)
        for cname in collections:
            spec = store.spec(cname)
            ws = verify[spec.title[:31]]
            ids = [r[0] for r in ws.iter_rows(min_row=2, max_col=1, values_only=True) if r[0]]
            if sorted(ids) != sorted(hashes[cname]["hashes"]):
                raise InvalidInput(f"export verification failed for {cname}: row IDs differ")
        verify.close()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    for cname in collections:
        store.db.execute("INSERT INTO excel_exports(path,collection,exported_at,row_hashes_json,db_versions_json) VALUES(?,?,?,?,?)"
                         " ON CONFLICT(path,collection) DO UPDATE SET exported_at=excluded.exported_at, row_hashes_json=excluded.row_hashes_json,"
                         " db_versions_json=excluded.db_versions_json",
                         (str(path.resolve()), cname, now(), canonical(hashes[cname]["hashes"]), canonical(hashes[cname]["versions"])))
    summary["verified"] = True
    return summary


def _stats_sheet(wb: Workbook, store: RecordStore, collections: list[str]) -> None:
    ws = wb.create_sheet("Statistics")
    ws["A1"], ws["A1"].font = "Statistics (live formulas)", Font(bold=True, size=14)
    row = 3
    for cname in collections:
        spec = store.spec(cname)
        sf = spec.field(spec.status_field) if spec.status_field else None
        if not sf or not sf.enum:
            continue
        idx = [f.name for f in spec.fields].index(sf.name) + 2
        letter = get_column_letter(idx)
        sheet = spec.title[:31].replace("'", "''")
        ws.cell(row=row, column=1, value=f"{spec.title} by {sf.name}").font = Font(bold=True)
        ws.cell(row=row + 1, column=1, value="Status").font = Font(bold=True)
        ws.cell(row=row + 1, column=2, value="Count").font = Font(bold=True)
        start = row + 2
        for i, s in enumerate(sf.enum):
            ws.cell(row=start + i, column=1, value=s)
            ws.cell(row=start + i, column=2, value=f"=COUNTIF('{sheet}'!{letter}:{letter},A{start + i})")
        end = start + len(sf.enum) - 1
        ws.cell(row=end + 1, column=1, value="Total").font = Font(bold=True)
        ws.cell(row=end + 1, column=2, value=f"=SUM(B{start}:B{end})")
        chart = BarChart()
        chart.title, chart.y_axis.title, chart.height, chart.width = f"{spec.title} by status", "Count", 7, 14
        chart.add_data(Reference(ws, min_col=2, min_row=start - 1, max_row=end), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=start, max_row=end))
        ws.add_chart(chart, f"D{row}")
        row = max(end + 3, row + 16)
    ws.column_dimensions["A"].width = 28


def import_collection(store: RecordStore, collection: str, path: Path, actor: str = "excel_import") -> dict:
    path = Path(path).expanduser()
    ensure_not_open(path)
    spec = store.spec(collection)
    exp = store.db.one("SELECT * FROM excel_exports WHERE path=? AND collection=?", (str(path.resolve()), collection))
    base_h = json.loads(exp["row_hashes_json"]) if exp else {}
    base_v = json.loads(exp["db_versions_json"]) if exp else {}
    wb = load_workbook(path, data_only=True)
    ws = wb[spec.title[:31]]
    header = [c.value for c in ws[1]]
    result = {"applied": [], "created": [], "conflicts": [], "invalid": [], "unchanged": 0}
    for values in ws.iter_rows(min_row=2, values_only=True):
        if not any(v not in (None, "") for v in values):
            continue
        row = dict(zip(header, values))
        rid = row.pop("ID", None)
        row.pop("_row_hash", None)
        data = {f.name: _norm_val(row.get(f.name)) for f in spec.fields if f.name in row}
        try:
            if not rid:
                r = store.upsert(collection, data, actor)
                result["created"].append(r["id"])
                continue
            cur = store.get(collection, rid)
            excel_h = row_hash(spec, data)
            if rid in base_h and excel_h == base_h[rid]:
                result["unchanged"] += 1
                continue
            if cur and rid in base_v and cur["_version"] != base_v[rid]:
                db_view = {k: v for k, v in cur.items() if not k.startswith("_")}
                if row_hash(spec, db_view) == excel_h:
                    result["unchanged"] += 1
                    continue
                store.db.execute("INSERT INTO record_conflicts(collection,record_id,excel_json,db_json,detected_at) VALUES(?,?,?,?,?)",
                                 (collection, rid, canonical(data), canonical(db_view), now()))
                result["conflicts"].append(rid)
                continue
            r = store.upsert(collection, {"id": rid, **data}, actor)
            if r["changed"]:
                result["applied"].append(rid)
            else:
                result["unchanged"] += 1
        except InvalidInput as e:
            result["invalid"].append({"id": rid, "error": str(e)})
    wb.close()
    return result


# ---- generic workbook operations (any user workbook) ------------------------------------------

def inspect_workbook(path: Path, max_cells: int = 40) -> dict:
    path = Path(path).expanduser()
    wb = load_workbook(path, data_only=False)
    out: dict[str, Any] = {"path": str(path), "sheets": []}
    for ws in wb.worksheets:
        formulas = []
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    formulas.append({"cell": c.coordinate, "formula": c.value})
                    if len(formulas) >= max_cells:
                        break
            if len(formulas) >= max_cells:
                break
        header = [c.value for c in ws[1]] if ws.max_row >= 1 else []
        out["sheets"].append({"name": ws.title, "dimensions": ws.dimensions, "max_row": ws.max_row, "max_col": ws.max_column,
                              "header": header, "tables": list(ws.tables.keys()), "charts": len(getattr(ws, "_charts", [])),
                              "images": len(getattr(ws, "_images", [])), "formulas_sample": formulas,
                              "data_validations": len(ws.data_validations.dataValidation),
                              "conditional_formats": len(ws.conditional_formatting)})
    wb.close()
    return out


def read_range(path: Path, sheet: str | None, cell_range: str | None, max_rows: int = 200) -> dict:
    wb = load_workbook(Path(path).expanduser(), data_only=True, read_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = []
    it = ws[cell_range] if cell_range else ws.iter_rows(max_row=max_rows)
    if cell_range and not isinstance(it, tuple):
        it = ((it,),)
    for r in it:
        rows.append([_norm_val(c.value) for c in (r if isinstance(r, tuple) else (r,))])
        if len(rows) >= max_rows:
            break
    wb.close()
    return {"sheet": ws.title, "rows": rows}


def write_cells(path: Path, sheet: str, cells: dict[str, Any]) -> dict:
    path = Path(path).expanduser()
    ensure_not_open(path)
    info = inspect_workbook(path)
    s = next((x for x in info["sheets"] if x["name"] == sheet), None)
    if s is None:
        raise InvalidInput(f"sheet {sheet!r} not found; sheets: {[x['name'] for x in info['sheets']]}")
    rich = any(x["charts"] or x["images"] for x in info["sheets"])
    bk = backup(path)
    if rich:
        _write_cells_com(path, sheet, cells)
        method = "com"
    else:
        wb = load_workbook(path)
        ws = wb[sheet]
        for addr, v in cells.items():
            ws[addr].value = v      # assigning .value keeps the cell's existing style
        wb.save(path)
        method = "openpyxl"
    wb = load_workbook(path, data_only=False, read_only=True)
    ws = wb[sheet]
    bad = [a for a, v in cells.items() if _norm_val(ws[a].value) != _norm_val(v)]
    wb.close()
    if bad:
        raise InvalidInput(f"post-write verification failed for cells {bad}; backup at {bk}")
    return {"written": len(cells), "method": method, "backup": str(bk) if bk else None, "verified": True}


def _write_cells_com(path: Path, sheet: str, cells: dict[str, Any]) -> None:
    """Excel COM keeps charts/pivots intact. Runs a private, invisible Excel instance (DispatchEx)."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    xl = win32com.client.DispatchEx("Excel.Application")
    xl.Visible, xl.DisplayAlerts = False, False
    try:
        wb = xl.Workbooks.Open(str(path.resolve()))
        ws = wb.Worksheets(sheet)
        for addr, v in cells.items():
            ws.Range(addr).Value = v
        wb.Save()
        wb.Close(SaveChanges=False)
    finally:
        xl.Quit()
        pythoncom.CoUninitialize()
