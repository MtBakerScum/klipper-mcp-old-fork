"""HelixScreen's per-head filament on the Snapmaker U1 (tracker #68).

From HelixScreen's source (prestonbrown/helixscreen, ams_backend_snapmaker.cpp and
filament_slot_override_store.cpp):
  - Each head's filament as set in Helix (brand, spool name, material, colour, Spoolman id,
    weights, temps) is kept in Moonraker's database, namespace "lane_data", one record per head
    under key "T<n>" (0-based; older records may use "lane<n>", 1-based). The record's own
    "lane" field is the 0-based head. OrcaSlicer reads the same namespace.
  - Helix also writes the firmware's record (print_task_config, what the U1's runout refill,
    RESUME and the stock screen use) with SET_PRINT_FILAMENT_CONFIG. That record only holds
    vendor (Snapmaker, or "Generic" for every other brand), type, sub-type (one of the
    firmware's own list, else "Basic") and colour. So "Generic PETG" there is normal.
  - Helix holds that write while a print is running and skips heads whose record came from an
    RFID tag. A held write that never landed leaves print_task_config stale: that is what this
    module detects, and sync_lines() writes it the way Helix does.
"""
from __future__ import annotations

import re

NAMESPACE = "lane_data"
HEADS = 4
KNOWN_SUBTYPES = ("Basic", "Matte", "SnapSpeed", "Silk", "Support", "HF", "95A", "95A HF")
PRODUCT_LINE_VENDOR = "Snapmaker"
SOFT = ("TPU", "TPE", "PEBA", "PVA", "FLEX")
_SAFE = re.compile(r"^[A-Za-z0-9 +_.\-]{1,32}$")


def _at(lst, i, default=None):
    return lst[i] if isinstance(lst, list) and 0 <= i < len(lst) else default


def _head_of(key: str, rec: dict) -> int | None:
    lane = rec.get("lane")
    try:
        if lane is not None and str(lane).strip() != "":
            return int(str(lane).strip())
    except ValueError:
        pass
    if key.startswith("T") and key[1:].isdigit():
        return int(key[1:])
    if key.startswith("lane") and key[4:].isdigit():
        return int(key[4:]) - 1
    return None


def _hex(c) -> str | None:
    if not isinstance(c, str):
        return None
    c = c.strip().lstrip("#")
    return c[:6].upper() if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", c) else None


def parse(doc: dict | None) -> dict[int, dict]:
    """Moonraker lane_data namespace -> {head index (0-based): Helix's filament for it}."""
    out: dict[int, dict] = {}
    for key, rec in (doc or {}).items():
        if not isinstance(rec, dict):
            continue
        h = _head_of(str(key), rec)
        if h is None or not 0 <= h < HEADS:
            continue
        mat = rec.get("helix_material") or rec.get("material") or ""
        out[h] = {k: v for k, v in {
            "brand": rec.get("vendor_name") or rec.get("vendor") or "",
            "name": rec.get("name") or rec.get("spool_name") or "",
            "material": mat,
            "color": _hex(rec.get("color")),
            "color_name": rec.get("color_name") or None,
            "spool_id": rec.get("spool_id") or None,
            "remaining_g": rec.get("remaining_weight_g"),
            "nozzle_temp": rec.get("nozzle_temp"),
            "bed_temp": rec.get("bed_temp"),
            "updated": rec.get("scan_time"),
        }.items() if v not in (None, "")}
    return out


def label(rec: dict | None) -> str:
    if not rec:
        return "nothing set in Helix"
    parts = [rec.get("brand"), rec.get("name"), rec.get("material")]
    words: list[str] = []
    for p in parts:  # "Polymaker" + "ASA Black" + "ASA" -> "Polymaker ASA Black"
        if p and p.lower() not in " ".join(words).lower().split() and p.lower() not in " ".join(words).lower():
            words.append(p)
    return " ".join(words) + (f" #{rec['color']}" if rec.get("color") else "")


def firmware_record(ptc: dict, i: int) -> dict:
    col = (_at(ptc.get("filament_color_multi"), i, {}) or {}).get("colors") or []
    return {"vendor": _at(ptc.get("filament_vendor"), i, "NONE"),
            "type": _at(ptc.get("filament_type"), i, "NONE"),
            "sub_type": _at(ptc.get("filament_sub_type"), i, "NONE"),
            "color": _hex(col[0]) if col else None,
            "official": bool(_at(ptc.get("filament_official"), i, False))}


def differences(fw: dict, rec: dict | None) -> list[str]:
    """What in the firmware record disagrees with Helix's choice for the head."""
    if not rec or not rec.get("material"):
        return []
    diffs = []
    if (fw.get("type") or "NONE").upper() != rec["material"].upper():
        diffs.append(f"type {fw.get('type')} (Helix: {rec['material']})")
    if rec.get("color") and fw.get("color") and fw["color"] != rec["color"]:
        diffs.append(f"colour #{fw['color']} (Helix: #{rec['color']})")
    if rec.get("color") and not fw.get("color"):
        diffs.append(f"no colour (Helix: #{rec['color']})")
    return diffs


def vendor_subtype(rec: dict) -> tuple[str, str]:
    """As Helix writes them: Snapmaker or Generic; a firmware sub-type or Basic."""
    vendor = PRODUCT_LINE_VENDOR if (rec.get("brand") or "").lower() == PRODUCT_LINE_VENDOR.lower() else "Generic"
    sub = rec.get("name") if rec.get("name") in KNOWN_SUBTYPES else "Basic"
    return vendor, sub


def _q(v: str) -> str:
    return f'"{v}"' if " " in v else v


def sync_lines(i: int, rec: dict) -> list[str] | str:
    """SET_PRINT_FILAMENT_CONFIG lines that make head i's firmware record match Helix, or why not."""
    mat = rec.get("material") or ""
    if not _SAFE.match(mat):
        return f"material '{mat}' can't be sent as a G-code parameter"
    vendor, sub = vendor_subtype(rec)
    soft = " SOFT=1" if any(mat.upper().startswith(s) for s in SOFT) else ""
    lines = [f"SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER={i} VENDOR={vendor} FILAMENT_TYPE={_q(mat)} "
             f"FILAMENT_SUBTYPE={_q(sub)}{soft}"]
    if rec.get("color"):
        lines.append(f"SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER={i} FILAMENT_COLOR_RGBA={rec['color']}FF")
    return lines


def summarize(ptc: dict, helix_doc: dict | None, detected: dict[int, bool] | None = None) -> dict:
    recs = parse(helix_doc)
    heads, stale = [], []
    for i in range(HEADS):
        fw = firmware_record(ptc, i)
        rec = recs.get(i)
        diffs = differences(fw, rec)
        row = {"head": i + 1, "helix": rec, "filament": label(rec), "firmware": fw}
        if detected is not None:
            row["filament_detected"] = bool(detected.get(i))
        if diffs:
            row["firmware_stale"] = diffs
            if fw["official"]:
                row["note"] = "firmware record came from an RFID tag; Helix leaves those alone"
            else:
                stale.append(i + 1)
        heads.append(row)
    out = {"source": "HelixScreen (Moonraker lane_data)" if recs else "printer only (no HelixScreen records)",
           "heads": heads, "stale_heads": stale}
    if stale:
        out["fix"] = ("the printer's own record (used for runout refill and RESUME) disagrees with Helix on "
                      f"head(s) {', '.join(map(str, stale))}; sync_firmware_filaments() rewrites it from Helix")
    return out
