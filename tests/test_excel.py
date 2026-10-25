import pytest
from openpyxl import load_workbook

from forge.errors import InvalidInput, ResourceUnavailable
from forge.records import excel as xl

COLL = """
collections:
  - name: apps
    title: Applications
    id_prefix: JOB
    key_fields: [employer, role, job_url]
    status_field: status
    initial_status: Discovered
    transitions:
      Discovered: [Shortlisted, Rejected]
      Shortlisted: [Submitted, Rejected]
      Submitted: [Interview, Rejected]
    fields:
      - {name: employer, type: string, required: true}
      - {name: role, type: string, required: true}
      - {name: job_url, type: url, required: true}
      - {name: score, type: integer, min: 0, max: 100}
      - {name: closing_date, type: date}
      - {name: status, type: enum, enum: [Discovered, Shortlisted, Submitted, Interview, Rejected]}
      - {name: notes, type: text}
"""


@pytest.fixture
def store(services):
    services.records.load_yaml(COLL)
    return services.records


def test_validation_ids_and_transitions(store):
    r = store.upsert("apps", {"employer": "Atlassian", "role": "Grad SWE", "job_url": "https://x.com/1", "score": 80}, "t")
    assert r["id"].startswith("JOB-") and r["created"]
    again = store.upsert("apps", {"employer": "atlassian ", "role": "grad swe", "job_url": "https://x.com/1"}, "t")
    assert again["id"] == r["id"]                       # stable ID from key fields
    with pytest.raises(InvalidInput):
        store.upsert("apps", {"employer": "A", "role": "B", "job_url": "ftp://bad"}, "t")
    with pytest.raises(InvalidInput):
        store.upsert("apps", {"employer": "A", "role": "B", "job_url": "https://x.com/2", "score": 101}, "t")
    with pytest.raises(InvalidInput, match="illegal status transition"):
        store.upsert("apps", {"id": r["id"], "status": "Interview"}, "t")
    store.upsert("apps", {"id": r["id"], "status": "Shortlisted"}, "t")
    assert [e["field"] for e in store.history("apps", r["id"])][0] == "status"


def test_export_formatting_and_verify(store, tmp_path):
    for i in range(5):
        store.upsert("apps", {"employer": f"E{i}", "role": "R", "job_url": f"https://x.com/{i}", "score": 50 + i, "closing_date": "2026-11-30"}, "t")
    p = tmp_path / "tracker.xlsx"
    res = xl.export_collections(store, ["apps"], p)
    assert res["verified"] and res["sheets"]["Applications"] == 5
    wb = load_workbook(p)
    ws = wb["Applications"]
    assert ws.tables and ws.data_validations.dataValidation and ws.freeze_panes == "B2"
    assert {"Activity Log", "Statistics"} <= set(wb.sheetnames)
    assert wb["Statistics"]["B5"].value.startswith("=COUNTIF")
    # second export takes a backup
    res2 = xl.export_collections(store, ["apps"], p)
    assert res2["backup"]


def test_import_three_way_merge_and_conflict(store, tmp_path):
    a = store.upsert("apps", {"employer": "A", "role": "R", "job_url": "https://x.com/a"}, "t")["id"]
    b = store.upsert("apps", {"employer": "B", "role": "R", "job_url": "https://x.com/b"}, "t")["id"]
    p = tmp_path / "t.xlsx"
    xl.export_collections(store, ["apps"], p)
    wb = load_workbook(p)
    ws = wb["Applications"]
    hdr = [c.value for c in ws[1]]
    notes, status = hdr.index("notes") + 1, hdr.index("status") + 1
    for row in ws.iter_rows(min_row=2):
        if row[0].value == a:
            row[notes - 1].value = "edited in excel"          # Excel-only edit
        if row[0].value == b:
            row[notes - 1].value = "excel says this"          # both sides edit -> conflict
    ws.append([None, "C", "New", "https://x.com/c"])           # new row typed by the user
    wb.save(p)
    store.upsert("apps", {"id": b, "notes": "forge says that"}, "workflow")
    res = xl.import_collection(store, "apps", p)
    assert res["applied"] == [a] and res["conflicts"] == [b] and len(res["created"]) == 1
    assert store.get("apps", a)["notes"] == "edited in excel"
    assert store.get("apps", b)["notes"] == "forge says that"   # DB not overwritten on conflict


def test_refuses_when_open_in_excel(store, tmp_path):
    p = tmp_path / "t.xlsx"
    xl.export_collections(store, ["apps"], p)
    xl.lock_file(p).write_text("x")
    with pytest.raises(ResourceUnavailable):
        xl.export_collections(store, ["apps"], p)


def test_write_cells_preserves_style_and_verifies(store, tmp_path):
    p = tmp_path / "t.xlsx"
    store.upsert("apps", {"employer": "A", "role": "R", "job_url": "https://x.com/a"}, "t")
    xl.export_collections(store, ["apps"], p, stats=False)
    before = load_workbook(p)["Applications"]["B1"].fill.fgColor.rgb
    res = xl.write_cells(p, "Applications", {"B1": "Employer!"})
    assert res["verified"] and res["method"] == "openpyxl"
    cell = load_workbook(p)["Applications"]["B1"]
    assert cell.value == "Employer!" and cell.fill.fgColor.rgb == before
