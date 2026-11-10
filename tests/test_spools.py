import json, os, shutil
import httpx, respx, pytest
from klipper_mcp import spools
import klipper_mcp.server as srv

REAL = "/mnt/user-data/uploads/Model Library/U1 Spool Tracker.xlsx"

LOADS = {
    "20261015_h1_0": {"time": "2026-10-15", "head": 1, "spool_id": "A", "action": "loaded"},
    "20261020_h1_0": {"time": "2026-10-20T10:00:00", "head": 1, "spool_id": "B", "action": "loaded"},
    "20261021_h2_0": {"time": "2026-10-21T10:00:00", "head": 2, "spool_id": "B", "action": "backup"},
}


def test_heads_follow_the_load_log():
    assert spools.spools_on_heads_at(LOADS, "2026-10-18T00:00:00") == {1: "A"}
    assert spools.heads_now(LOADS) == {2: "B"}   # B moved to head 2; head 1 left empty


def test_job_usage_prorates_cancelled():
    job = {"status": "cancelled", "filament_used": 500.0,
           "metadata": {"filament_weights": [10.0, 0, 20.0], "filament_total": 1000.0}}
    assert spools.job_usage(job) == [{"head": 1, "grams": 5.0, "source": "prorated"},
                                     {"head": 3, "grams": 10.0, "source": "prorated"}]
    assert spools.job_usage({"status": "completed", "metadata": {"filament_weight_total": 7}})[0]["grams"] == 7


def test_view_remaining_and_stock():
    sp = {"A": {"material": "PLA", "start_g": 1000, "tracked_from": "2026-10-15", "status": "Active"},
          "E": {"material": "PLA", "start_g": 1000, "status": "Empty"}}
    us = {"j1_h1": {"time": "2026-10-16", "spool_id": "A", "grams": 100},
          "j0_h1": {"time": "2026-10-14", "spool_id": "A", "grams": 999}}   # before tracking: ignored
    rows = spools.view(sp, us, {})
    a = next(r for r in rows if r["id"] == "A")
    assert a["used_g"] == 100 and a["remaining_g"] == 900
    assert spools.stock(rows) == [{"material": "PLA", "spools": 1, "grams": 900.0, "unknown_weight": 0}]


def test_new_usage_once_per_job():
    jobs = [{"job_id": "0000A1", "status": "completed", "filename": "x.gcode", "start_time": 1790000000,
             "end_time": 1790003600, "metadata": {"filament_weights": [12.5]}}]
    ld = {"k": {"time": "2026-01-01", "head": 1, "spool_id": "A", "action": "loaded"}}
    rows = spools.new_usage_rows(jobs, {}, ld)
    assert list(rows) == ["0000A1_h1"] and rows["0000A1_h1"]["spool_id"] == "A"
    assert spools.new_usage_rows(jobs, rows, ld) == {}


@pytest.mark.skipif(not os.path.exists(REAL), reason="real spreadsheet not staged")
def test_import_real_spreadsheet_round_trip(tmp_path):
    d = spools.import_workbook(REAL)
    assert len(d["spools"]) >= 10 and len(d["usage"]) > 100 and d["loads"]
    out = tmp_path / "back.xlsx"
    spools.export_workbook(str(out), d["spools"], d["usage"], d["loads"])
    d2 = spools.import_workbook(str(out))
    assert set(d2["spools"]) == set(d["spools"])
    assert len(d2["usage"]) == len(d["usage"])


B = "http://x:7125"


class FakeDB:
    def __init__(self):
        self.ns = {}

    def handler(self, request):
        if request.method == "GET":
            ns = request.url.params["namespace"]
            if ns not in self.ns:
                return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})
            return httpx.Response(200, json={"result": {"namespace": ns, "value": self.ns[ns]}})
        if request.method == "POST":
            b = json.loads(request.content)
            self.ns.setdefault(b["namespace"], {})[b["key"]] = b["value"]
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(405)


def _env(m, db, jobs=()):
    m.setenv("MOONRAKER_URL", B); m.setenv("MOONRAKER_FALLBACK_URL", "")
    respx.route(url__startswith=f"{B}/server/database/item").mock(side_effect=db.handler)
    respx.get(f"{B}/server/history/list").mock(return_value=httpx.Response(200, json={"result": {"jobs": list(jobs)}}))


@respx.mock
async def test_save_load_list_and_auto_usage(monkeypatch):
    db = FakeDB()
    job = {"job_id": "0000B2", "status": "completed", "filename": "y.gcode", "start_time": 1790000000,
           "end_time": 1790003600, "metadata": {"filament_weights": [40.0]}}
    _env(monkeypatch, db, [job])
    assert (await srv.save_spool("BLK-PLA-02", brand="Sunlu", material="PLA", start_g=1000))["created"]
    assert (await srv.load_spool(1, "BLK-PLA-02"))["on_heads"] == {"1": "BLK-PLA-02"}
    db.ns[spools.NS_LOADS]["20260101_h1_9"] = {"time": "2026-01-01", "head": 1, "spool_id": "BLK-PLA-02", "action": "loaded"}
    db.ns[spools.NS_SPOOLS]["BLK-PLA-02"]["tracked_from"] = "2026-01-01"
    out = await srv.list_spools()
    assert out["spools"][0]["remaining_g"] == 960.0 and out["logged_now"][0]["grams"] == 40.0
    again = await srv.list_spools()
    assert again["logged_now"] == [] and again["spools"][0]["remaining_g"] == 960.0
    w = await srv.weigh_spool("BLK-PLA-02", 1150, empty_spool_g=250)
    assert w["remaining_g"] == 900.0
    assert (await srv.list_spools())["spools"][0]["remaining_g"] == 900.0


@respx.mock
@pytest.mark.skipif(not os.path.exists(REAL), reason="real spreadsheet not staged")
async def test_import_gated_then_refused_when_full(monkeypatch, tmp_path):
    db = FakeDB()
    _env(monkeypatch, db)
    src = tmp_path / "t.xlsx"; shutil.copy(REAL, src)
    assert (await srv.import_spools(str(src)))["confirm_required"]
    assert (await srv.import_spools(str(src), confirm=True))["ok"]
    assert (await srv.import_spools(str(src), confirm=True))["error"] == "inventory_not_empty"
    lst = await srv.list_spools(include_empty=True)
    assert len(lst["spools"]) >= 10 and lst["stock"]


def test_import_keeps_bracketed_ids_and_rows_without_job_id(tmp_path):
    import openpyxl
    from klipper_mcp import spools as sp
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Spools"
    ws.append(["Spool ID", "Brand", "Material", "Start g (at tracking start)"])
    ws.append(["(Polymaker ASA Black)", "Polymaker", "ASA", 1000])
    ws.append(["BLK-TPU-01", "Overture", "TPU", 1000])
    u = wb.create_sheet("Usage log")
    u.append(["Date", "Job ID", "G-code", "Result", "Head", "Spool ID", "Grams", "Source", "Notes"])
    u.append(["2026-10-01", "—", "a", "completed", 2, "BLK-TPU-01", 59.77, "hand", None])
    u.append(["2026-10-02", "—", "b", "completed", 2, "BLK-TPU-01", 10.0, "hand", None])
    u.append(["2026-10-03", "0000A1", "c", "completed", 3, "(Polymaker ASA Black)", 5.0, "slicer", None])
    ld = wb.create_sheet("Loads")
    ld.append(["Date", "Head", "Spool ID", "Action", "Notes"])
    ld.append(["2026-10-01", 3, "(Polymaker ASA Black)", "loaded", None])
    f = tmp_path / "t.xlsx"
    wb.save(f)
    d = sp.import_workbook(str(f))
    assert "Polymaker-ASA-Black" in d["spools"]
    assert d["renamed"] == {"(Polymaker ASA Black)": "Polymaker-ASA-Black"}
    rows = {r["id"]: r for r in sp.view(d["spools"], d["usage"], d["loads"])}
    assert rows["BLK-TPU-01"]["used_g"] == 69.8
    assert rows["Polymaker-ASA-Black"]["used_g"] == 5.0
    assert rows["Polymaker-ASA-Black"]["head"] == 3


def test_rename_and_delete_spool(monkeypatch):
    import asyncio
    from klipper_mcp import server, spools as sp

    db = {sp.NS_SPOOLS: {"A-01": {"brand": "X", "material": "PLA"}, "B-01": {"material": "PETG"}},
          sp.NS_USAGE: {"j1_h1": {"spool_id": "A-01", "grams": 5}},
          sp.NS_LOADS: {"20261101T000000_h1_0": {"time": "2026-11-01T00:00:00", "head": 1, "spool_id": "A-01", "action": "loaded"}},
          "lane_data": {"T0": {"material": "PLA", "snorca_spool": "A-01"}}}

    class C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def database_namespace(self, ns): return db.get(ns)
        async def database_put(self, ns, k, v): db.setdefault(ns, {})[k] = v
        async def database_delete(self, ns, k): db.get(ns, {}).pop(k, None)

    monkeypatch.setattr(server, "_client", lambda: C())
    pv = asyncio.run(server.rename_spool("A-01", "A-PLA-X-01"))
    assert pv["preview"]["usage_rows"] == 1 and "A-01" in db[sp.NS_SPOOLS]
    r = asyncio.run(server.rename_spool("A-01", "A-PLA-X-01", confirm=True))
    assert r["ok"] and "A-PLA-X-01" in db[sp.NS_SPOOLS] and "A-01" not in db[sp.NS_SPOOLS]
    assert db[sp.NS_USAGE]["j1_h1"]["spool_id"] == "A-PLA-X-01"
    assert db["lane_data"]["T0"]["snorca_spool"] == "A-PLA-X-01"
    assert asyncio.run(server.delete_spool("A-PLA-X-01", confirm=True))["error"] == "on_head"
    d = asyncio.run(server.delete_spool("B-01", confirm=True))
    assert d["ok"] and "B-01" not in db[sp.NS_SPOOLS]
    assert any(v["what"] == "delete" for v in db["snorca_spool_history"].values())
