import json
import httpx, respx
from klipper_mcp import helix
import klipper_mcp.server as srv

LANE = {
    "T0": {"lane": "0", "material": "PETG", "helix_material": "PETG", "vendor_name": "Atomic", "name": "Marble",
           "color": "#D8D8D0", "spool_id": 7, "remaining_weight_g": 640},
    "T1": {"lane": "1", "material": "TPU", "vendor_name": "Overture", "name": "TPU", "color": "#202020"},
    "T2": {"lane": "2", "material": "ASA", "vendor_name": "Polymaker", "name": "ASA Black", "color": "#000000"},
    "lane4": {"material": "PETG", "vendor_name": "Atomic", "name": "Amethyst", "color": "#7A4FA0"},
}
PTC = {"filament_vendor": ["Generic", "Generic", "NONE", "NONE"],
       "filament_type": ["PETG", "TPU", "NONE", "NONE"],
       "filament_sub_type": ["Basic", "Basic", "NONE", "NONE"],
       "filament_color_multi": [{"colors": ["D8D8D0FF"]}, {"colors": ["202020FF"]}, {"colors": ["FFFFFF"]}, {"colors": ["FFFFFF"]}],
       "filament_official": [False, False, False, False]}


def test_parse_keys_and_fields():
    r = helix.parse(LANE)
    assert set(r) == {0, 1, 2, 3}
    assert r[0]["brand"] == "Atomic" and r[0]["color"] == "D8D8D0" and r[0]["spool_id"] == 7
    assert r[3]["name"] == "Amethyst"   # lane4 = head 4 (1-based key)
    assert helix.label(r[2]) == "Polymaker ASA Black #000000"


def test_summary_flags_stale_heads():
    s = helix.summarize(PTC, LANE)
    assert s["stale_heads"] == [3, 4]
    assert s["heads"][0].get("firmware_stale") is None   # Generic PETG matches Atomic Marble PETG
    assert "type NONE" in s["heads"][2]["firmware_stale"][0]


def test_sync_lines_like_helix():
    r = helix.parse(LANE)
    assert helix.sync_lines(2, r[2]) == [
        "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=2 VENDOR=Generic FILAMENT_TYPE=ASA FILAMENT_SUBTYPE=Basic",
        "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=2 FILAMENT_COLOR_RGBA=000000FF"]
    assert "SOFT=1" in helix.sync_lines(1, r[1])[0]
    snap = {"brand": "Snapmaker", "name": "SnapSpeed", "material": "PLA"}
    assert "VENDOR=Snapmaker FILAMENT_TYPE=PLA FILAMENT_SUBTYPE=SnapSpeed" in helix.sync_lines(0, snap)[0]
    assert isinstance(helix.sync_lines(0, {"material": "PLA;M112"}), str)


def test_no_helix_records():
    s = helix.summarize(PTC, None)
    assert s["stale_heads"] == [] and s["source"].startswith("printer only")


B = "http://x:7125"
OBJS = ["print_task_config", "print_stats", "filament_feed left", "filament_feed right"]


def _env(m, lane=LANE, state="standby"):
    m.setenv("MOONRAKER_URL", B); m.setenv("MOONRAKER_FALLBACK_URL", "")
    respx.get(f"{B}/printer/info").mock(return_value=httpx.Response(200, json={"result": {"state": "ready"}}))
    respx.get(f"{B}/printer/objects/list").mock(return_value=httpx.Response(200, json={"result": {"objects": OBJS}}))
    st = {"print_task_config": PTC, "print_stats": {"state": state, "filename": ""},
          "filament_feed left": {"extruder0": {"filament_detected": True}, "extruder1": {"filament_detected": True}},
          "filament_feed right": {"extruder2": {"filament_detected": True}, "extruder3": {"filament_detected": True}}}
    respx.get(url__regex=rf"{B}/printer/objects/query.*").mock(
        return_value=httpx.Response(200, json={"result": {"status": st}}))
    if lane is None:
        respx.get(f"{B}/server/database/item").mock(return_value=httpx.Response(404, json={"error": {"code": 404, "message": "Namespace 'lane_data' not found"}}))
    else:
        respx.get(f"{B}/server/database/item").mock(
            return_value=httpx.Response(200, json={"result": {"namespace": "lane_data", "value": lane}}))


@respx.mock
async def test_get_loaded_filaments(monkeypatch):
    _env(monkeypatch)
    out = await srv.get_loaded_filaments()
    assert out["supported"] and out["stale_heads"] == [3, 4]
    assert out["heads"][0]["filament"].startswith("Atomic Marble PETG") and out["heads"][0]["filament_detected"]


@respx.mock
async def test_get_loaded_filaments_without_helix(monkeypatch):
    _env(monkeypatch, lane=None)
    out = await srv.get_loaded_filaments()
    assert out["supported"] and out["stale_heads"] == []


@respx.mock
async def test_sync_previews_then_sends(monkeypatch):
    _env(monkeypatch)
    prev = await srv.sync_firmware_filaments()
    assert prev["confirm_required"] and "CONFIG_EXTRUDER=2 VENDOR=Generic FILAMENT_TYPE=ASA" in prev["preview"]["gcode"]
    assert "CONFIG_EXTRUDER=3" in prev["preview"]["gcode"] and "CONFIG_EXTRUDER=0" not in prev["preview"]["gcode"]
    sent = respx.post(f"{B}/printer/gcode/script").mock(return_value=httpx.Response(200, json={"result": "ok"}))
    out = await srv.sync_firmware_filaments(confirm=True)
    assert out["ok"] and sent.called


@respx.mock
async def test_sync_refused_while_printing(monkeypatch):
    _env(monkeypatch, state="printing")
    assert (await srv.sync_firmware_filaments(confirm=True))["error"] == "print_running"
