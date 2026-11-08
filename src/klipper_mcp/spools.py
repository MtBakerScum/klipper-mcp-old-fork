"""Spool inventory kept in Moonraker's database on the printer (tracker #69).

Shared by klipper-mcp, Snorca's Spools tab and anyone else on the network. Layout (no dots in
keys: Moonraker reads them as paths):
  namespace "snorca_spools"       key <spool id>              -> spool
  namespace "snorca_spool_usage"  key "<job id>_h<head>"      -> one head's grams for one job
  namespace "snorca_spool_loads"  key "<YYYYmmddTHHMMSS>_h<head>_<n>" -> load / unload / run-out
One entry per spool and per log row, so two writers never overwrite each other's changes.

spool:  {id, brand, material, colour, preset, start_g, tracked_from, status, notes,
         weighed_g, weighed_at}
        remaining = start_g - (usage logged for it from tracked_from on). A weigh-in sets
        start_g to the net weight and tracked_from to now.
usage:  {time, job_id, gcode, result, head, spool_id, grams, source, notes}
load:   {time, head, spool_id, action (loaded|backup|unloaded|ran out|weighed), notes}

Grams per job come from the slicer's per-filament weights in the job's metadata (tool N =
head N+1, the U1's default map), scaled by what the printer actually extruded for a cancelled
job: the same rule as the old spreadsheet. Each job is logged once per head, by job id.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

NS_SPOOLS = "snorca_spools"
NS_USAGE = "snorca_spool_usage"
NS_LOADS = "snorca_spool_loads"
HEADS = 4
ON_HEAD = ("loaded", "backup")
OFF_HEAD = ("unloaded", "ran out", "removed", "empty")
FINISHED = ("completed", "cancelled", "error", "klippy_shutdown", "klippy_disconnect", "interrupted")
SPOOL_FIELDS = ("brand", "material", "name", "colour", "preset", "start_g", "tracked_from", "status", "notes",
                "weighed_g", "weighed_at", "location")
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def iso_from_epoch(t) -> str | None:
    try:
        return datetime.fromtimestamp(float(t)).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def valid_id(sid: str) -> bool:
    return bool(_ID.match(sid or ""))


def load_key(t_iso: str, head: int | None, n: int = 0) -> str:
    stamp = re.sub(r"[^0-9T]", "", t_iso)[:15]
    return f"{stamp}_h{head if head else 0}_{n}"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def heads_now(loads: dict) -> dict[int, str]:
    """{head: spool id} from the load log, latest entry per head."""
    return spools_on_heads_at(loads, None)


def spools_on_heads_at(loads: dict, t_iso: str | None) -> dict[int, str]:
    on: dict[int, str] = {}
    for _, e in sorted((loads or {}).items(), key=lambda kv: (str(kv[1].get("time", "")), kv[0])):
        if t_iso and str(e.get("time", "")) > t_iso:
            break
        try:
            h = int(e.get("head"))
        except (TypeError, ValueError):
            continue
        act = str(e.get("action", "")).lower()
        sid = e.get("spool_id")
        if act in ON_HEAD and sid:
            for other, s in list(on.items()):  # a spool is on one head at a time
                if s == sid and other != h:
                    del on[other]
            on[h] = sid
        elif act in OFF_HEAD and on.get(h) == sid:
            del on[h]
        elif act in OFF_HEAD and not sid:
            on.pop(h, None)
    return on


def used_since(usage: dict, sid: str, since: str | None) -> float:
    tot = 0.0
    for u in (usage or {}).values():
        if u.get("spool_id") != sid:
            continue
        if since and str(u.get("time", "")) < str(since):
            continue
        tot += _num(u.get("grams")) or 0.0
    return round(tot, 1)


def view(spools: dict, usage: dict, loads: dict) -> list[dict]:
    on = {s: h for h, s in heads_now(loads).items()}
    out = []
    for sid, sp in sorted((spools or {}).items()):
        start = _num(sp.get("start_g"))
        used = used_since(usage, sid, sp.get("tracked_from"))
        row = {"id": sid, **{k: sp.get(k) for k in SPOOL_FIELDS if sp.get(k) not in (None, "")}}
        row["used_g"] = used
        row["remaining_g"] = None if start is None else round(max(start - used, 0.0), 1)
        if sid in on:
            row["head"] = on[sid]
        out.append(row)
    return out


def stock(rows: list[dict]) -> list[dict]:
    """Grams on hand per material (spools not marked Empty)."""
    agg: dict[str, dict] = {}
    for r in rows:
        if str(r.get("status", "")).lower() == "empty":
            continue
        m = r.get("material") or "?"
        a = agg.setdefault(m, {"material": m, "spools": 0, "grams": 0.0, "unknown_weight": 0})
        a["spools"] += 1
        if r.get("remaining_g") is None:
            a["unknown_weight"] += 1
        else:
            a["grams"] = round(a["grams"] + r["remaining_g"], 1)
    return sorted(agg.values(), key=lambda a: a["material"])


def job_usage(job: dict) -> list[dict]:
    """Per-head grams for one finished job: [{head, grams, source}], heads 1-based."""
    md = job.get("metadata") or {}
    weights = md.get("filament_weights")
    if isinstance(weights, (int, float)):
        weights = [weights]
    if not isinstance(weights, list) or not weights:
        tot = _num(md.get("filament_weight_total"))
        weights = [tot] if tot else []
    used_mm, total_mm = _num(job.get("filament_used")), _num(md.get("filament_total"))
    done = job.get("status") == "completed"
    scale, source = 1.0, "slicer"
    if not done:
        scale = min(1.0, used_mm / total_mm) if used_mm is not None and total_mm else 0.0
        source = "prorated"
    out = []
    for i, w in enumerate(weights[:HEADS]):
        g = _num(w)
        if g is None or g <= 0:
            continue
        out.append({"head": i + 1, "grams": round(g * scale, 1), "source": source})
    return out


def new_usage_rows(jobs: list[dict], usage: dict, loads: dict) -> dict[str, dict]:
    """Usage entries for finished jobs not logged yet: {key: entry}."""
    have = {str(u.get("job_id")) for u in (usage or {}).values()}
    rows: dict[str, dict] = {}
    for job in jobs:
        jid = str(job.get("job_id") or "")
        if not jid or jid in have or job.get("status") not in FINISHED:
            continue
        t = iso_from_epoch(job.get("end_time") or job.get("start_time")) or now_iso()
        t0 = iso_from_epoch(job.get("start_time")) or t
        on = spools_on_heads_at(loads, t0)
        for u in job_usage(job):
            sid = on.get(u["head"])
            rows[f"{jid}_h{u['head']}"] = {
                "time": t, "job_id": jid, "gcode": job.get("filename"), "result": job.get("status"),
                "head": u["head"], "spool_id": sid, "grams": u["grams"], "source": u["source"],
                **({} if sid else {"notes": f"no spool was loaded on head {u['head']} in the inventory"})}
    return rows


# --- import from the old spreadsheet (U1 Spool Tracker.xlsx) -------------------------------

def _d(v) -> str | None:
    if v in (None, "", "—"):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:19]
    return str(v).strip()


def import_workbook(path: str) -> dict:
    """{spools, usage, loads} ready for the database, from the Spool Tracker workbook."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)

    def rows(name):
        ws = wb[name]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h or "").strip() for h in next(it)]
        for r in it:
            if r and any(c not in (None, "") for c in r):
                yield dict(zip(hdr, r))

    spools, usage, loads = {}, {}, {}
    renamed: dict[str, str] = {}

    def fix_id(raw) -> str | None:
        """The sheet's spool ID as a database key: "(Polymaker ASA Black)" -> "Polymaker-ASA-Black"."""
        sid = str(raw or "").strip()
        if not sid or sid in ("—", "-"):
            return None
        if sid in renamed:
            return renamed[sid]
        if valid_id(sid):
            return sid
        new = re.sub(r"[^A-Za-z0-9_\-]+", "-", sid).strip("-")[:40] or None
        if new:
            renamed[sid] = new
        return new

    for r in rows("Spools"):
        sid = fix_id(r.get("Spool ID"))
        if not sid:
            continue
        loc = str(r.get("Current head") or "").strip()
        sp = {"brand": r.get("Brand"), "material": r.get("Material"), "colour": r.get("Colour"),
              "preset": r.get("Snorca preset"), "start_g": _num(r.get("Start g (at tracking start)")),
              "tracked_from": _d(r.get("Date opened / tracked from")), "status": r.get("Status"),
              "notes": r.get("Notes")}
        if loc and not loc.isdigit() and loc != "—":
            sp["location"] = loc
        w = r.get("Weighed g / date")
        if w not in (None, ""):
            sp["notes"] = f"{sp.get('notes') or ''} Weighed: {w}".strip()
        spools[sid] = {k: v for k, v in sp.items() if v not in (None, "")}
    for n_row, r in enumerate(rows("Usage log"), start=2):
        jid = str(r.get("Job ID") or "").strip()
        if jid in ("—", "-"):
            jid = ""
        head = r.get("Head")
        try:
            h = int(head)
        except (TypeError, ValueError):
            h = 0
        # Rows with no job ID (hand entries) are keyed by their sheet row, so none overwrites another.
        key = f"{jid}_h{h}" if jid else f"sheet{n_row}_h{h}"
        if key in usage:
            key = f"{key}_r{n_row}"
        usage[key] = {k: v for k, v in {
            "time": _d(r.get("Date")), "job_id": jid or None, "gcode": r.get("G-code"), "result": r.get("Result"),
            "head": h or None, "spool_id": fix_id(r.get("Spool ID")), "grams": _num(r.get("Grams")) or 0.0,
            "source": r.get("Source"), "notes": r.get("Notes")}.items() if v not in (None, "")}
    n = 0
    for r in rows("Loads"):
        t = _d(r.get("Date")) or ""
        head = r.get("Head")
        try:
            h = int(head)
        except (TypeError, ValueError):
            h = None
        loads[load_key(t, h, n)] = {k: v for k, v in {
            "time": t, "head": h, "spool_id": fix_id(r.get("Spool ID")), "action": str(r.get("Action") or "").lower(),
            "notes": r.get("Notes")}.items() if v not in (None, "")}
        n += 1
    return {"spools": spools, "usage": usage, "loads": loads, "renamed": renamed}


def export_workbook(path: str, spools: dict, usage: dict, loads: dict) -> None:
    """Write the inventory back out in the old spreadsheet's layout (the backup copy)."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Spools"
    ws.append(["Spool ID", "Brand", "Material", "Colour", "Snorca preset", "Start g (at tracking start)",
               "Date opened / tracked from", "Current head", "Status", "Used g (since start)", "Remaining g",
               "Weighed g / date", "Notes"])
    for r in view(spools, usage, loads):
        w = f"{r['weighed_g']} / {r.get('weighed_at', '')}" if r.get("weighed_g") is not None else None
        ws.append([r["id"], r.get("brand"), r.get("material"), r.get("colour"), r.get("preset"), r.get("start_g"),
                   r.get("tracked_from"), r.get("head") or r.get("location") or "—", r.get("status"),
                   r["used_g"], r["remaining_g"], w, r.get("notes")])
    ws = wb.create_sheet("Usage log")
    ws.append(["Date", "Job ID", "G-code", "Result", "Head", "Spool ID", "Grams", "Source", "Notes"])
    for _, u in sorted(usage.items(), key=lambda kv: (str(kv[1].get("time", "")), kv[0])):
        ws.append([u.get("time"), u.get("job_id"), u.get("gcode"), u.get("result"), u.get("head"),
                   u.get("spool_id"), u.get("grams"), u.get("source"), u.get("notes")])
    ws = wb.create_sheet("Loads")
    ws.append(["Date", "Head", "Spool ID", "Action", "Notes"])
    for _, e in sorted(loads.items(), key=lambda kv: (str(kv[1].get("time", "")), kv[0])):
        ws.append([e.get("time"), e.get("head"), e.get("spool_id"), e.get("action"), e.get("notes")])
    ws = wb.create_sheet("Read me")
    for line in ("U1 Spool Tracker (backup copy)",
                 "The inventory lives in Moonraker's database on the printer (snorca_spools / snorca_spool_usage / "
                 "snorca_spool_loads). This file is written from it by klipper-mcp's export_spools; edits here are "
                 "NOT read back.",
                 f"Written {now_iso()}."):
        ws.append([line])
    wb.save(path)
