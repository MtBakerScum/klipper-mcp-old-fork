from __future__ import annotations
from pathlib import Path
import re
import importlib.metadata
from mcp.server.mcpserver import MCPServer, Image
from mcp.types import ToolAnnotations
from .config import load_config
from .client import MoonrakerClient
from .errors import ApiError, NoReply, Refused, PrinterNotReady
from . import gate
from . import outcomes
from . import runout
from . import offsets
from . import helix
from . import spools
from .status import summarize_status, STATUS_OBJECTS

try:
    _VERSION = importlib.metadata.version("klipper-mcp")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "0.0.0"

mcp = MCPServer("klipper", version=_VERSION)

RESULT_MAP = {"completed": "success", "cancelled": "cancelled"}  # everything else -> "error"


def _client() -> MoonrakerClient:
    return MoonrakerClient(load_config())


def _err(e: ApiError) -> dict:
    return {"error": str(e)}


def summarize_job(job: dict) -> dict:
    meta = job.get("metadata") or {}
    dur = job.get("total_duration")
    return {
        "job_id": job.get("job_id"),
        "filename": job.get("filename"),
        "result": RESULT_MAP.get(job.get("status"), "error"),
        "moonraker_status": job.get("status"),
        "ended_at": job.get("end_time"),
        "duration_min": None if dur is None else int(round(dur / 60)),
        "filament_g": None if meta.get("filament_weight_total") is None else round(meta["filament_weight_total"], 1),
        "layer_height": meta.get("layer_height"),
        "filament_name": meta.get("filament_name"),
    }


@mcp.tool()
async def get_printer_status() -> dict:
    """Live printer state in one call: Klipper state (ready/shutdown/error + message), current job
    (state, file, progress, durations), the active extruder, actual/target temps for every extruder
    (extruder, extruder1..3 on toolchangers) and the bed, fan, pressure advance of the active tool,
    live Z offset, speed/flow factors, toolhead position. Read-only. Use this before any control."""
    try:
        async with _client() as c:
            return summarize_status(await c.objects_query(STATUS_OBJECTS))
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_printer_sensors() -> dict:
    """Every sensor, fan and output the printer reports, in one call: heaters (temp, target, power %
    for each extruder, the bed and any heater_generic such as a chamber heater), temperature sensors
    (chamber/cavity, MCU, host...), stepper-driver temperatures, fans (part fan, every fan_generic /
    heater_fan / controller_fan / temperature_fan with speed %), output pins and LEDs, filament
    runout sensors, plus Snapmaker/paxx extras (purifier, print_task_config, DragonBreath) under
    "other". Read-only. Use it to answer "what is running and how hot is it"."""
    from . import sensors
    try:
        async with _client() as c:
            names = [n for n in await c.objects_list() if sensors.wanted(n)]
            return sensors.summarize(await c.objects_query_named(names)) if names else {}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def query_objects(names: list[str]) -> dict:
    """Raw Klipper object state for the named objects (as Moonraker's printer/objects/query returns
    it), e.g. ["temperature_sensor cavity", "fan_generic exhaust"]. With names=[] it lists every
    object the printer has. Read-only; for a readable summary use get_printer_sensors."""
    try:
        async with _client() as c:
            if not names:
                return {"objects": await c.objects_list()}
            return {"status": await c.objects_query_named(names)}
    except ApiError as e:
        return _err(e)


def _cfg_path_ok(path: str) -> str | None:
    p = (path or "").replace("\\", "/").lstrip("/")
    if not p or ".." in p.split("/") or not p.lower().endswith((".cfg", ".conf")):
        return None
    return p


_READABLE = (".cfg", ".conf", ".json", ".env", ".txt", ".ini", ".yaml", ".yml", ".toml", ".md", ".log", ".sh", ".py")


def _readable_path_ok(path: str) -> str | None:
    p = (path or "").replace("\\", "/").lstrip("/")
    if not p or ".." in p.split("/") or not p.lower().endswith(_READABLE):
        return None
    return p


@mcp.tool()
async def list_config_files(all_files: bool = False) -> dict:
    """List the printer's config files (Moonraker's "config" root: printer.cfg, moonraker.conf and,
    on the U1's extended firmware, extended/klipper/*.cfg). all_files=true lists every file and
    folder there (e.g. helixscreen/ with HelixScreen's own settings and user_filaments.json),
    with sizes. Read-only."""
    try:
        async with _client() as c:
            files = await c.config_files()
        if all_files:
            return {"files": sorted(({"path": f.get("path"), "size": f.get("size")} for f in files),
                                    key=lambda f: str(f["path"]))}
        return {"files": sorted(f.get("path") for f in files
                                if str(f.get("path", "")).lower().endswith((".cfg", ".conf")))}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def read_config_file(path: str) -> dict:
    """Read one file under the config root by its path from list_config_files (e.g.
    "extended/klipper/exhaust.cfg", "helixscreen/user_filaments.json"): .cfg/.conf and other text
    files (.json, .env, .txt, .ini, .yaml, .toml, .md, .log, .sh, .py). Read-only."""
    p = _readable_path_ok(path)
    if p is None:
        return {"error": "bad_path", "hint": "a text file path from list_config_files(all_files=true), no '..'"}
    try:
        async with _client() as c:
            text = await c.config_read(p)
        return {"path": p, "lines": text.count("\n") + 1, "text": text}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def write_config_file(path: str, text: str, confirm: bool = False) -> dict:
    """Replace a config file's contents (.cfg/.conf under the config root). Before writing, the
    current file is copied to <path>.bak-<timestamp> next to it. Nothing is reloaded: Klipper
    reads config only on restart (on the U1, Grant hard-reboots after a .cfg change), and
    moonraker.conf needs a Moonraker restart. TWO-PHASE: without confirm=true this returns a
    unified diff preview and changes nothing; show it to the user, then call again with
    confirm=true."""
    import difflib, datetime
    p = _cfg_path_ok(path)
    if p is None:
        return {"error": "bad_path", "hint": "a .cfg/.conf path, no '..'"}
    try:
        async with _client() as c:
            try:
                old = await c.config_read(p)
                exists = True
            except ApiError:
                old, exists = "", False
            diff = "".join(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                                f"a/{p}", f"b/{p}"))
            if not confirm:
                return gate.confirm_required("write_config_file", {"path": p, "exists": exists,
                                                                   "diff": diff or "(no change)"})
            if old == text:
                return {"ok": True, "path": p, "changed": False}
            backup = None
            if exists:
                backup = f"{p}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
                await c.config_copy(p, backup)
            await c.config_write(p, text)
        return {"ok": True, "path": p, "changed": True, "backup": backup,
                "note": "not reloaded; restart Klipper (U1: hard reboot) to apply"}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_printer_info() -> dict:
    """Klipper host info: state and the full state message (e.g. why it is shut down and how to
    recover), Klipper version, hostname. Read-only."""
    try:
        async with _client() as c:
            info = await c.printer_info()
        return {"state": info.get("state"), "message": (info.get("state_message") or "").strip(),
                "software_version": info.get("software_version"), "hostname": info.get("hostname")}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def list_print_history(limit: int = 10) -> dict:
    """Recent print jobs from Moonraker's history, newest first: result (success/cancelled/error),
    duration, filament grams, layer height, filament preset. Read-only."""
    try:
        async with _client() as c:
            jobs = await c.history_list(limit=max(1, min(limit, 100)))
        return {"count": len(jobs), "jobs": [summarize_job(j) for j in jobs]}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_job_filament(job_id: str | None = None) -> dict:
    """Filament used PER HEAD by a finished or cancelled print job (default: the most recent one):
    mm and grams per tool, from the job's G-code walked up to the amount the printer actually
    extruded (so a cancelled job counts only what was printed). Use it to log spools. Grams need
    the filament_diameter/density Orca writes into the G-code. Read-only; downloads the G-code."""
    from .gcode_usage import per_tool_usage
    try:
        async with _client() as c:
            if job_id:
                job = await c.history_job(job_id)
            else:
                jobs = await c.history_list(limit=1)
                job = jobs[0] if jobs else {}
            if not job:
                return {"error": "no_job", "hint": "no print history yet, or unknown job_id"}
            fname = job.get("filename")
            actual = job.get("filament_used")
            data = await c.download("gcodes", fname)
        usage = per_tool_usage(data.decode("utf-8", "replace").splitlines(),
                               stop_at_mm=float(actual) if actual else None)
        return {"job_id": job.get("job_id"), "filename": fname, "status": job.get("status"),
                "actual_total_mm": round(float(actual), 1) if actual else None, **usage,
                "note": "per-head split from the G-code; total matches the printer's own count"}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def list_timelapses(limit: int = 20) -> dict:
    """Timelapse videos on the printer, newest first (any Moonraker file root holding .mp4/.mkv
    files, e.g. "timelapse"). Read-only. Use save_timelapse to copy one to this PC."""
    try:
        async with _client() as c:
            roots = [r.get("name") for r in await c.file_roots() if r.get("name")]
            vids = []
            for root in roots:
                if root in ("config", "logs", "config_examples", "docs"):
                    continue
                try:
                    files = await c.files_list(root)
                except ApiError:
                    continue
                for f in files:
                    if str(f.get("path", "")).lower().endswith((".mp4", ".mkv", ".avi")):
                        vids.append({"root": root, "path": f.get("path"), "size": f.get("size"),
                                     "modified": f.get("modified")})
        vids.sort(key=lambda v: v.get("modified") or 0, reverse=True)
        return {"count": len(vids[:limit]), "timelapses": vids[:max(1, limit)]}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def save_timelapse(path: str, root: str = "timelapse", frames: int = 0):
    """Copy a timelapse from the printer (path/root from list_timelapses) to this PC and return
    where it was saved. frames=N (max 8) also returns N evenly spaced stills to look at, when
    ffmpeg is installed on this PC; otherwise just the file. Read-only on the printer."""
    import os, shutil, subprocess, tempfile
    from pathlib import Path as _P
    try:
        async with _client() as c:
            data = await c.download(root, path, timeout=300.0)
    except ApiError as e:
        return _err(e)
    out_dir = _P(os.environ.get("KLIPPER_MCP_DOWNLOADS", str(_P.home() / "Downloads" / "printer-timelapses")))
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / _P(path).name
    dest.write_bytes(data)
    meta = {"saved": str(dest), "bytes": len(data)}
    frames = max(0, min(int(frames), 8))
    if frames == 0:
        return meta
    ff = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    if not ff or not probe:
        return {**meta, "frames": "ffmpeg not found on this PC; only the file was saved"}
    try:
        dur = float(subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of",
                                    "default=nw=1:nk=1", str(dest)], capture_output=True, text=True,
                                   timeout=30).stdout.strip() or 0)
    except (ValueError, subprocess.SubprocessError):
        dur = 0
    images = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(frames):
            t = dur * (i + 0.5) / frames if dur > 0 else i
            fp = _P(td) / f"f{i}.jpg"
            subprocess.run([ff, "-v", "error", "-ss", f"{t:.2f}", "-i", str(dest), "-frames:v", "1",
                            "-vf", "scale=960:-2", str(fp)], capture_output=True, timeout=60)
            if fp.exists():
                images.append(Image(data=fp.read_bytes(), format="jpeg"))
    return [*images, {**meta, "frames_returned": len(images), "duration_s": round(dur, 1)}]


@mcp.tool()
async def wait_for_height(z_mm: float, timeout_s: float = 600, snapshot: bool = True):
    """Wait until the running print reaches height z_mm (toolhead Z, mm above the bed), then
    return the status and, by default, a camera snapshot. Use it for checks at specific heights
    (support tops, clip mounts) instead of guessing times. Returns early if the print ends,
    pauses or errors, and after timeout_s (max 1200) with reached=false - call it again to keep
    waiting. Read-only."""
    import asyncio, time
    timeout_s = max(5.0, min(float(timeout_s), 1200.0))
    deadline = time.monotonic() + timeout_s
    hits = 0
    try:
        async with _client() as c:
            while True:
                q = await c.objects_query(["toolhead", "print_stats", "display_status"])
                z = ((q.get("toolhead") or {}).get("position") or [0, 0, 0])[2]
                job = (q.get("print_stats") or {}).get("state")
                prog = (q.get("display_status") or {}).get("progress")
                # Two polls in a row, so a Z-hop or travel lift doesn't count as reaching it.
                hits = hits + 1 if (z is not None and z >= z_mm) else 0
                reached = hits >= 2
                done = job not in ("printing",)
                if reached or done or time.monotonic() >= deadline:
                    info = {"reached": bool(reached), "z": round(z or 0, 2), "target_z": z_mm,
                            "job_state": job, "progress_pct": round((prog or 0) * 100, 1)}
                    break
                await asyncio.sleep(5)
    except ApiError as e:
        return _err(e)
    if snapshot and info["reached"]:
        snap = await get_camera_snapshot()
        if isinstance(snap, list):
            return [snap[0], {**info, "camera": snap[1].get("camera")}]
    return info


@mcp.tool()
async def list_gcode_files(limit: int = 50) -> dict:
    """G-code files staged on the printer (Moonraker 'gcodes' root), newest first. Read-only."""
    try:
        async with _client() as c:
            files = await c.files_list("gcodes")
        files = sorted(files, key=lambda f: f.get("modified", 0), reverse=True)
        shown = files[:max(1, limit)]
        return {"count": len(shown), "total": len(files),
                "files": [{"path": f.get("path"), "size": f.get("size"), "modified": f.get("modified")}
                          for f in shown]}
    except ApiError as e:
        return _err(e)


_TEMP_LINE = re.compile(r"^\s*(ok\s+)?(B|T\d?):\s*-?\d", re.I)


@mcp.tool()
async def get_console(count: int = 50, pattern: str | None = None, include_temps: bool = False) -> dict:
    """Recent Klipper console lines (what Fluidd/Mainsail show in the console): commands sent and
    responses/errors, oldest first. count = lines to return after filtering (1..500). pattern = optional
    case-insensitive regex to keep only matching lines. Temperature report lines are dropped unless
    include_temps=true. Read-only."""
    try:
        count = max(1, min(count, 500))
        rx = re.compile(pattern, re.I) if pattern else None
        async with _client() as c:
            store = await c.gcode_store(count=1000)
        lines, seen = [], set()
        for e in store:
            msg = (e.get("message") or "").strip()
            # Moonraker can store the same entry several times (same time/type/message); keep one.
            key = (e.get("time"), e.get("type"), msg)
            if key in seen:
                continue
            seen.add(key)
            if not include_temps and _TEMP_LINE.match(msg):
                continue
            if rx and not rx.search(msg):
                continue
            lines.append({"time": e.get("time"), "type": e.get("type"), "message": msg})
        lines = lines[-count:]
        return {"count": len(lines), "lines": lines}
    except re.error as e:
        return {"error": f"bad_pattern: {e}"}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_power_devices() -> dict:
    """Moonraker power devices (e.g. exhaust fan / lights / smart plugs set up under [power]) and
    their on/off state. Read-only."""
    try:
        async with _client() as c:
            devs = await c.power_devices()
        return {"count": len(devs), "devices": [
            {"device": d.get("device"), "status": d.get("status"), "type": d.get("type"),
             "locked_while_printing": d.get("locked_while_printing")} for d in devs]}
    except ApiError as e:
        return _err(e)


# Tried when Moonraker has no webcam configured (paths seen on crowsnest / paxx firmware).
_SNAPSHOT_FALLBACKS = ("/webcam/?action=snapshot", "/webcam/snapshot.jpg", "/webcam/snapshot")


@mcp.tool()
async def get_camera_snapshot(camera: str | None = None):
    """Take a still picture from the printer's camera and look at it: check the first layer,
    spaghetti, a lifted corner, whether the plate is clear before a print, or progress. Uses the
    cameras configured in Moonraker (the ones Fluidd shows); pass camera= to pick one by name,
    default the first. Returns the image plus which camera and URL it came from. Read-only; it
    doesn't move or light anything (turn the chamber light on first if the picture is dark)."""
    try:
        async with _client() as c:
            cams = []
            try:
                cams = await c.webcams()
            except ApiError:
                pass  # older Moonraker without the webcam list: fall back to the usual paths
            root = c.web_root()
            if camera is not None:
                pick = [w for w in cams if (w.get("name") or "").lower() == camera.lower()]
                if not pick:
                    return {"error": f"no camera named {camera!r}",
                            "cameras": [w.get("name") for w in cams]}
                cams = pick
            urls: list[tuple[str, str]] = []
            for w in cams:
                u = (w.get("snapshot_url") or "").strip()
                if u:
                    urls.append((w.get("name") or "camera", u if "://" in u else root + "/" + u.lstrip("/")))
            if camera is None:
                urls += [("default", root + p) for p in _SNAPSHOT_FALLBACKS]
            tried = []
            for name, url in urls:
                try:
                    data, ctype = await c.get_bytes(url)
                except ApiError as e:
                    tried.append(f"{url}: {e}")
                    continue
                if not ctype.startswith("image/") or not data:
                    tried.append(f"{url}: not an image ({ctype or 'no content type'})")
                    continue
                fmt = ctype.split("/", 1)[1].split(";", 1)[0].strip() or "jpeg"
                return [Image(data=data, format=fmt),
                        {"camera": name, "url": url, "bytes": len(data),
                         "cameras": [w.get("name") for w in cams]}]
            return {"error": "no snapshot available", "tried": tried}
    except ApiError as e:
        return _err(e)


def summarize_mesh(bm: dict) -> dict:
    m = bm.get("probed_matrix") or []
    vals = [v for row in m for v in row]
    if not vals:
        return {"profile": bm.get("profile_name") or None, "loaded": False,
                "profiles": sorted((bm.get("profiles") or {}).keys())}
    rows, cols = len(m), len(m[0])
    mean = lambda xs: sum(xs) / len(xs)
    r = lambda x: round(x, 4)
    return {
        "profile": bm.get("profile_name"), "loaded": True,
        "profiles": sorted((bm.get("profiles") or {}).keys()),
        "mesh_min": bm.get("mesh_min"), "mesh_max": bm.get("mesh_max"),
        "points": f"{cols}x{rows}",
        "min": r(min(vals)), "max": r(max(vals)), "range": r(max(vals) - min(vals)), "mean": r(mean(vals)),
        # row 0 is the front (min Y), column 0 is the left (min X)
        "front_mean": r(mean(m[0])), "back_mean": r(mean(m[-1])),
        "left_mean": r(mean([row[0] for row in m])), "right_mean": r(mean([row[-1] for row in m])),
        "corners": {"front_left": r(m[0][0]), "front_right": r(m[0][-1]),
                    "back_left": r(m[-1][0]), "back_right": r(m[-1][-1])},
        "probed_matrix": [[r(v) for v in row] for row in m],
    }


@mcp.tool()
async def get_bed_mesh() -> dict:
    """The active bed mesh: profile, probed grid, min/max/range/mean, front/back/left/right averages
    and corner heights (to spot tilt), plus saved profile names. Row 0 = front, column 0 = left.
    Read-only."""
    try:
        async with _client() as c:
            st = await c.objects_query(["bed_mesh"])
        return summarize_mesh(st.get("bed_mesh") or {})
    except ApiError as e:
        return _err(e)


_HEATERS = {"extruder": "extruder", "extruder1": "extruder1", "extruder2": "extruder2",
            "extruder3": "extruder3", "bed": "heater_bed", "heater_bed": "heater_bed"}
_GCODE_SUFFIXES = {".gcode", ".gco", ".g", ".ufp"}


async def _ready_and_status(c) -> tuple[dict, dict]:
    """Return (printer_info, status_summary); raise PrinterNotReady if Klipper is not ready."""
    info = await c.printer_info()
    gate.require_ready(info)
    return info, summarize_status(await c.objects_query(STATUS_OBJECTS))


def _job_preview(st: dict) -> dict:
    return {"filename": st["job"]["filename"], "job_state": st["job"]["state"],
            "progress_percent": st["job"]["progress_percent"]}


def _local_gcode_tools(path: Path) -> list[int] | None:
    """Logical tools a local G-code file extrudes with (tracker #50)."""
    from .gcode_usage import per_tool_usage
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            usage = per_tool_usage(fh)
    except OSError:
        return None
    return [r["tool"] for r in usage["tools"] if r["mm"] > 1.0] or None


async def _runout_state(c, filename: str | None = None, tools: list[int] | None = None) -> dict:
    """Tracker #50: which used heads have a runout backup the U1 would switch to."""
    names = await c.objects_list()
    if "print_task_config" not in names:
        return {"supported": False}
    feeds_n = [n for n in names if n.startswith("filament_feed")]
    want = ["print_task_config"] + [n for n in names if n in {runout.extruder_obj(i) for i in range(runout.HEADS)}
                                    or n in {runout.sensor_obj(i) for i in range(runout.HEADS)}] + feeds_n
    q = await c.objects_query_named(want)
    ptc = q.get("print_task_config") or {}
    feeds: dict = {}
    for n in feeds_n:
        feeds.update(q.get(n) or {})
    sensors = {k: v for k, v in q.items() if k.startswith("filament_motion_sensor")}
    extruders = {k: v for k, v in q.items() if k == "extruder" or (k.startswith("extruder") and k[8:].isdigit())}
    if not tools and filename:
        meta = await c.file_metadata(filename) or {}
        rt = meta.get("referenced_tools")
        if isinstance(rt, list) and rt:
            tools = [int(t) for t in rt if isinstance(t, (int, float))]
    heads = runout.used_heads(ptc, tools)
    rep = runout.report(ptc, feeds, sensors, extruders, heads)
    # Tracker #68: name heads by what HelixScreen has set, and say when the printer's own record
    # (which the refill actually matches on) disagrees with it.
    try:
        hx = helix.summarize(ptc, await c.database_namespace(helix.NAMESPACE))
        by_head = {r["head"]: r for r in hx["heads"]}
        for row in rep["heads"]:
            hr = by_head.get(row["head"]) or {}
            if hr.get("helix"):
                row["helix_filament"] = hr["filament"]
            if hr.get("firmware_stale"):
                row["firmware_stale"] = hr["firmware_stale"]
        used = {r["head"] for r in rep["heads"]}
        stale = [h for h in hx["stale_heads"] if h in used]
        if stale:
            rep["warnings"].append(
                f"the printer's filament record for head(s) {', '.join(map(str, stale))} doesn't match "
                f"HelixScreen; the runout refill matches on the printer's record, so backups above may be "
                f"wrong. sync_firmware_filaments() fixes it (not during a print)")
    except Exception:  # best effort: Helix may not be installed
        pass
    rep["used_heads_from"] = "file" if tools else "printer"
    rep["empty_used_heads"] = [h["head"] for h in rep["heads"] if not h["loaded"]]
    return {"supported": True, **rep}


@mcp.tool()
async def check_runout_backups(path: str | None = None) -> dict:
    """Snapmaker U1: for each head a job uses, whether the printer has a backup head it would switch
    to automatically when that head runs out (same filament vendor/type/sub-type, same colour
    unless the printer's ignore-colour option is on, same nozzle size and flow type, filament
    loaded). A head with no backup pauses the print on runout, the extruder map can't be changed
    while paused, and resuming needs filament loaded into that same head.
    path: a G-code file (local, or on the printer) to take the used heads from; otherwise the current or last
    job's heads (or every head with filament set). Read-only."""
    try:
        async with _client() as c:
            if path and Path(path).is_file():
                return await _runout_state(c, None, _local_gcode_tools(Path(path)))
            return await _runout_state(c, path or None)
    except ApiError as e:
        return _err(e)


_FEED_OBJS = ("filament_feed left", "filament_feed right")


async def _filament_state(c) -> dict:
    names = await c.objects_list()
    if "print_task_config" not in names:
        return {"supported": False}
    want = ["print_task_config", "print_stats"] + [n for n in names if n.startswith("filament_feed")]
    q = await c.objects_query_named(want)
    feeds: dict = {}
    for n, v in q.items():
        if n.startswith("filament_feed"):
            feeds.update(v or {})
    detected = {i: bool((feeds.get(f"extruder{i}") or {}).get("filament_detected")) for i in range(helix.HEADS)}
    out = helix.summarize(q.get("print_task_config") or {}, await c.database_namespace(helix.NAMESPACE), detected)
    out["supported"] = True
    out["job_state"] = (q.get("print_stats") or {}).get("state")
    return out


@mcp.tool()
async def get_loaded_filaments() -> dict:
    """Snapmaker U1: the filament on each head as set in HelixScreen (brand, spool name, material,
    colour, Spoolman spool, remaining grams, temps; Moonraker's lane_data database), next to the
    printer's own record of it (print_task_config: vendor Snapmaker/Generic, type, sub-type,
    colour; what runout refill, RESUME and the stock screen use) and whether the feeder sees
    filament. Heads where the two disagree are listed in stale_heads; HelixScreen holds its write
    to the printer during a print, so it can lag. Use this, not print_task_config, to say what is
    loaded. Read-only."""
    try:
        async with _client() as c:
            return await _filament_state(c)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def sync_firmware_filaments(heads: list[int] | None = None, confirm: bool = False) -> dict:
    """Snapmaker U1: rewrite the printer's filament record (print_task_config) for heads where it
    disagrees with HelixScreen, the same way HelixScreen does (SET_PRINT_FILAMENT_CONFIG: vendor
    Snapmaker or Generic, type, sub-type or Basic, colour). Fixes runout refill matching and
    RESUME after Helix's write didn't land. heads: 1-4 (default: every stale head). Refused while a
    print is running or paused (the firmware refuses it mid-print, and each write resets that
    head's flow K). Skips heads whose record came from an RFID tag. TWO-PHASE: without
    confirm=true this returns a preview and changes nothing."""
    try:
        async with _client() as c:
            await _ready_and_status(c)
            st = await _filament_state(c)
            if not st.get("supported"):
                return {"supported": False}
            if st.get("job_state") in ("printing", "paused"):
                return {"error": "print_running", "reason": "the printer refuses filament record changes during a "
                        "print; run this when it is idle"}
            want = set(heads) if heads else set(st["stale_heads"])
            if any(not isinstance(h, int) or not 1 <= h <= helix.HEADS for h in want):
                return {"error": f"heads are 1-{helix.HEADS}"}
            lines, skipped = [], []
            for row in st["heads"]:
                h = row["head"]
                if h not in want:
                    continue
                if not row.get("helix") or not row["helix"].get("material"):
                    skipped.append({"head": h, "why": "no material set in HelixScreen"})
                    continue
                if row["firmware"].get("official"):
                    skipped.append({"head": h, "why": "record came from an RFID tag"})
                    continue
                got = helix.sync_lines(h - 1, row["helix"])
                if isinstance(got, str):
                    skipped.append({"head": h, "why": got})
                else:
                    lines += got
            if not lines:
                return {"ok": True, "nothing_to_do": True, "skipped": skipped,
                        "heads": [{"head": r["head"], "filament": r["filament"]} for r in st["heads"]]}
            script = "\n".join(lines)
            if not confirm:
                return gate.confirm_required("sync_firmware_filaments", {"gcode": script, "skipped": skipped})
            await c.gcode_script(script)
            after = await _filament_state(c)
        return {"ok": True, "sent": script, "skipped": skipped, "stale_heads_now": after.get("stale_heads")}
    except (PrinterNotReady, ApiError) as e:
        return _err(e)


# --- spool inventory (tracker #69) -------------------------------------------------------

async def _inventory(c, sync: bool = True) -> dict:
    """The inventory, after logging any finished jobs not counted yet (once each, by job id)."""
    sp = await c.database_namespace(spools.NS_SPOOLS) or {}
    us = await c.database_namespace(spools.NS_USAGE) or {}
    ld = await c.database_namespace(spools.NS_LOADS) or {}
    logged = []
    if sync and sp:
        jobs = await c.history_list(limit=50)
        rows = spools.new_usage_rows(jobs, us, ld)
        for k, v in rows.items():
            await c.database_put(spools.NS_USAGE, k, v)
            us[k] = v
            logged.append({k2: v.get(k2) for k2 in ("job_id", "head", "spool_id", "grams", "result")})
    return {"spools": sp, "usage": us, "loads": ld, "logged_now": logged}


@mcp.tool()
async def list_spools(status: str | None = None, material: str | None = None, include_empty: bool = False) -> dict:
    """The spool inventory (kept in Moonraker's database on the printer, shared with Snorca's
    Spools tab): each spool's brand, material, colour, Snorca preset, grams left, grams used,
    status and the head it's on, plus stock (grams on hand per material). Finished jobs not yet
    counted are logged first (grams per head from the slicer, prorated for cancelled jobs; each
    job once). status / material filter (case-insensitive); empty spools are left out unless
    include_empty. Read-only apart from that usage logging."""
    try:
        async with _client() as c:
            inv = await _inventory(c)
        rows = spools.view(inv["spools"], inv["usage"], inv["loads"])
        if not inv["spools"]:
            return {"spools": [], "note": "the inventory is empty; import_spools(path) loads the old spreadsheet"}
        stock = spools.stock(rows)
        if not include_empty:
            rows = [r for r in rows if str(r.get("status", "")).lower() != "empty"]
        if status:
            rows = [r for r in rows if str(r.get("status", "")).lower() == status.lower()]
        if material:
            rows = [r for r in rows if material.lower() in str(r.get("material", "")).lower()]
        heads = {h: s for h, s in spools.heads_now(inv["loads"]).items()}
        return {"spools": rows, "stock": stock, "on_heads": {str(h): heads.get(h) for h in range(1, spools.HEADS + 1)},
                "logged_now": inv["logged_now"]}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def save_spool(spool_id: str, brand: str | None = None, material: str | None = None,
                     colour: str | None = None, preset: str | None = None, start_g: float | None = None,
                     status: str | None = None, notes: str | None = None, location: str | None = None,
                     name: str | None = None, price: float | None = None, size_g: float | None = None,
                     abrasive: bool | None = None) -> dict:
    """Add a spool or change its fields (only the ones given). spool_id: e.g. "BLK-PLA-02"
    (letters, digits, - and _). start_g: net filament grams when tracking starts (nominal 1000
    for a new 1 kg spool); a new spool's tracking starts now. status: Active / Sealed / Empty.
    location: where it is when not on a head (shelf, dryer...). name: the spool's own name as
    HelixScreen shows it ("Marble", "90A"); Snorca's Spools tab sends it to HelixScreen when the
    spool goes on a head, and Snorca's Sync matches presets by brand + name. price: what the spool cost
    (cost per kg = price / size_g * 1000; size_g defaults to 1000). abrasive: true/false overrides
    the guess from the material (CF/GF) and preset. Use weigh_spool for weigh-ins
    and load_spool for heads."""
    if not spools.valid_id(spool_id):
        return {"error": "spool_id: 1-40 letters, digits, - or _"}
    try:
        async with _client() as c:
            cur = (await c.database_namespace(spools.NS_SPOOLS) or {}).get(spool_id)
            sp = dict(cur or {})
            for k, v in {"brand": brand, "material": material, "colour": colour, "preset": preset,
                         "start_g": start_g, "status": status, "notes": notes, "location": location,
                         "name": name,
                         "price": price, "size_g": size_g, "abrasive": abrasive}.items():
                if v is not None:
                    sp[k] = v
            if cur is None:
                sp.setdefault("tracked_from", spools.now_iso())
                sp.setdefault("status", "Active")
            await c.database_put(spools.NS_SPOOLS, spool_id, sp)
        return {"ok": True, "created": cur is None, "spool": {"id": spool_id, **sp}}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def weigh_spool(spool_id: str, weight_g: float, empty_spool_g: float | None = None) -> dict:
    """Record a weigh-in: the spool's grams left become weight_g minus empty_spool_g (the empty
    reel; omit if weight_g is already net), and usage counts from now. Logged in the loads log."""
    try:
        async with _client() as c:
            sp = (await c.database_namespace(spools.NS_SPOOLS) or {}).get(spool_id)
            if sp is None:
                return {"error": "unknown_spool", "spool_id": spool_id}
            net = round(float(weight_g) - float(empty_spool_g or 0), 1)
            if net < 0:
                return {"error": "weight below the empty spool's weight"}
            t = spools.now_iso()
            sp.update({"start_g": net, "tracked_from": t, "weighed_g": net, "weighed_at": t})
            await c.database_put(spools.NS_SPOOLS, spool_id, sp)
            await c.database_put(spools.NS_LOADS, spools.load_key(t, None),
                                 {"time": t, "head": None, "spool_id": spool_id, "action": "weighed",
                                  "notes": f"{weight_g} g" + (f" - {empty_spool_g} g empty spool" if empty_spool_g else "")})
        return {"ok": True, "spool_id": spool_id, "remaining_g": net}
    except ApiError as e:
        return _err(e)


NS_SPOOL_HISTORY = "snorca_spool_history"  # the Spools tab's undo log (same records, so its Undo works)


def _hist_key(t_iso: str, sid: str) -> str:
    import re as _re
    return _re.sub(r"[^0-9T]", "", t_iso) + "_" + sid


@mcp.tool()
async def rename_spool(spool_id: str, new_id: str, confirm: bool = False) -> dict:
    """Rename a spool (change its ID). Its usage rows, load log and HelixScreen's note of which
    spool is on a head follow it; the Spools tab's Undo can put it back. new_id: letters, digits,
    - and _ (the tab's pattern: COLOUR-MATERIAL-BRAND-NN, e.g. BLK-PETGGF-ELG-01). TWO-PHASE:
    without confirm=true this returns a preview (what moves) and changes nothing."""
    if not spools.valid_id(new_id):
        return {"error": "bad_id", "hint": "1-40 letters, digits, - or _"}
    try:
        async with _client() as c:
            sp = await c.database_namespace(spools.NS_SPOOLS) or {}
            if spool_id not in sp:
                return {"error": "not_found", "spool_id": spool_id}
            if new_id in sp:
                return {"error": "exists", "new_id": new_id}
            usage = await c.database_namespace(spools.NS_USAGE) or {}
            loads = await c.database_namespace(spools.NS_LOADS) or {}
            lanes = await c.database_namespace("lane_data") or {}
            u_keys = [k for k, v in usage.items() if isinstance(v, dict) and v.get("spool_id") == spool_id]
            l_keys = [k for k, v in loads.items() if isinstance(v, dict) and v.get("spool_id") == spool_id]
            h_keys = [k for k, v in lanes.items() if isinstance(v, dict) and v.get("snorca_spool") == spool_id]
            plan = {"from": spool_id, "to": new_id, "usage_rows": len(u_keys), "load_rows": len(l_keys),
                    "helix_heads": h_keys}
            if not confirm:
                return {"preview": plan, "note": "nothing changed; call again with confirm=true"}
            for k in u_keys:
                await c.database_put(spools.NS_USAGE, k, {**usage[k], "spool_id": new_id})
            for k in l_keys:
                await c.database_put(spools.NS_LOADS, k, {**loads[k], "spool_id": new_id})
            for k in h_keys:
                await c.database_put("lane_data", k, {**lanes[k], "snorca_spool": new_id})
            rec = sp[spool_id]
            await c.database_put(spools.NS_SPOOLS, new_id, rec)
            await c.database_delete(spools.NS_SPOOLS, spool_id)
            t = spools.now_iso()
            await c.database_put(NS_SPOOL_HISTORY, _hist_key(t, new_id),
                                 {"time": t, "spool_id": new_id, "what": "rename", "from": spool_id, "before": rec})
        return {"ok": True, **plan}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def delete_spool(spool_id: str, confirm: bool = False) -> dict:
    """Delete a spool from the inventory (for ones entered by mistake or duplicates; use
    save_spool(status="Empty") for a finished spool). Its usage history stays; the Spools tab's
    Undo brings it back. Refused while it's on a head. TWO-PHASE: without confirm=true this returns
    a preview and changes nothing. Only delete when Grant says to."""
    try:
        async with _client() as c:
            sp = await c.database_namespace(spools.NS_SPOOLS) or {}
            if spool_id not in sp:
                return {"error": "not_found", "spool_id": spool_id}
            loads = await c.database_namespace(spools.NS_LOADS) or {}
            on = {s: h for h, s in spools.heads_now(loads).items()}
            if spool_id in on:
                return {"error": "on_head", "head": on[spool_id], "hint": "unload it first (load_spool action=unloaded)"}
            usage = await c.database_namespace(spools.NS_USAGE) or {}
            n_use = sum(1 for v in usage.values() if isinstance(v, dict) and v.get("spool_id") == spool_id)
            plan = {"spool": {"id": spool_id, **sp[spool_id]}, "usage_rows_kept": n_use}
            if not confirm:
                return {"preview": plan, "note": "nothing changed; call again with confirm=true"}
            t = spools.now_iso()
            await c.database_put(NS_SPOOL_HISTORY, _hist_key(t, spool_id),
                                 {"time": t, "spool_id": spool_id, "what": "delete", "before": sp[spool_id]})
            await c.database_delete(spools.NS_SPOOLS, spool_id)
        return {"ok": True, "deleted": spool_id, "usage_rows_kept": n_use}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def load_spool(head: int, spool_id: str | None = None, action: str = "loaded", notes: str | None = None) -> dict:
    """Record what's on a head: action "loaded" (spool_id goes on head 1-4, replacing whatever was
    there), "backup" (loaded as an auto-refill backup), "unloaded" or "ran out" (spool_id
    optional: whatever is on the head comes off; "ran out" also marks that spool Empty). Usage
    from later jobs goes to the spool on each head."""
    if not 1 <= head <= spools.HEADS:
        return {"error": f"head is 1-{spools.HEADS}"}
    action = action.lower()
    if action not in spools.ON_HEAD + ("unloaded", "ran out"):
        return {"error": "action: loaded | backup | unloaded | ran out"}
    try:
        async with _client() as c:
            sp_all = await c.database_namespace(spools.NS_SPOOLS) or {}
            ld = await c.database_namespace(spools.NS_LOADS) or {}
            on = spools.heads_now(ld)
            t = spools.now_iso()
            n = 0
            if action in spools.ON_HEAD:
                if spool_id not in sp_all:
                    return {"error": "unknown_spool", "spool_id": spool_id}
                prev = on.get(head)
                if prev and prev != spool_id:
                    await c.database_put(spools.NS_LOADS, spools.load_key(t, head, n),
                                         {"time": t, "head": head, "spool_id": prev, "action": "unloaded",
                                          "notes": f"replaced by {spool_id}"})
                    n += 1
            else:
                spool_id = spool_id or on.get(head)
                if action == "ran out" and spool_id in sp_all:
                    sp = dict(sp_all[spool_id]); sp["status"] = "Empty"
                    await c.database_put(spools.NS_SPOOLS, spool_id, sp)
            await c.database_put(spools.NS_LOADS, spools.load_key(t, head, n),
                                 {k: v for k, v in {"time": t, "head": head, "spool_id": spool_id, "action": action,
                                                    "notes": notes}.items() if v is not None})
            ld2 = await c.database_namespace(spools.NS_LOADS) or {}
        return {"ok": True, "on_heads": {str(h): s for h, s in spools.heads_now(ld2).items()}}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def import_spools(path: str, confirm: bool = False) -> dict:
    """One-time import of the old spool spreadsheet (U1 Spool Tracker.xlsx: Spools, Usage log,
    Loads) into the printer's inventory. Refused if the inventory already has spools. TWO-PHASE:
    without confirm=true it shows what would be imported."""
    try:
        data = spools.import_workbook(path)
    except Exception as e:  # noqa: BLE001
        return {"error": "cannot_read", "reason": str(e)}
    try:
        async with _client() as c:
            if await c.database_namespace(spools.NS_SPOOLS):
                return {"error": "inventory_not_empty", "reason": "the printer already has spools; nothing imported"}
            summary = {"spools": len(data["spools"]), "usage_rows": len(data["usage"]), "load_rows": len(data["loads"]),
                       "ids": sorted(data["spools"])}
            if data.get("renamed"):
                summary["renamed_ids"] = data["renamed"]  # sheet ID -> database ID (brackets, spaces...)
            if not confirm:
                return gate.confirm_required("import_spools", summary)
            for ns, rows in ((spools.NS_SPOOLS, data["spools"]), (spools.NS_USAGE, data["usage"]),
                             (spools.NS_LOADS, data["loads"])):
                for k, v in rows.items():
                    await c.database_put(ns, k, v)
        return {"ok": True, **summary}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def export_spools(path: str) -> dict:
    """Write the inventory to a spreadsheet in the old U1 Spool Tracker layout, as the backup copy
    (an existing file is first copied to <name>.bak-<time>.xlsx), and ask Moonraker to back up its
    database too (newer Moonraker only), since a firmware reflash may wipe it."""
    import shutil
    try:
        async with _client() as c:
            inv = await _inventory(c)
            stamp = spools.load_key(spools.now_iso(), None)[:15]
            db = await c.database_backup(f"snorca-spools-{stamp}.db")
        p = Path(path)
        backup = None
        if p.exists():
            backup = p.with_name(f"{p.stem}.bak-{stamp}{p.suffix}")
            shutil.copy2(p, backup)
        spools.export_workbook(str(p), inv["spools"], inv["usage"], inv["loads"])
        return {"ok": True, "written": str(p), "previous_copied_to": str(backup) if backup else None,
                "moonraker_backup": db if db is not None else "not supported by this Moonraker",
                "spools": len(inv["spools"]), "usage_rows": len(inv["usage"])}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_head_offsets() -> dict:
    """Snapmaker U1: each head's XYZ offset from head 1 (what the offset calibration measured and
    the printer applies at every tool change), with nozzle size/type, plus the printer's main
    state and calibration step (to see whether a calibration is running). Read-only."""
    try:
        async with _client() as c:
            q = await c.objects_query_named(offsets.QUERY)
        if "extruder_offset_calibration" not in q:
            return {"supported": False, "reason": "no extruder_offset_calibration object (not a Snapmaker U1?)"}
        return {"supported": True, **offsets.summarize(q)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def calibrate_head_offsets(phase: str, confirm: bool = False, heads: list[int] | None = None,
                                 clean: bool = True, bed_temp: int | None = None) -> dict:
    """Snapmaker U1: run the head offset (XYZ) calibration without the touchscreen, in the
    firmware's own order. Three phases, each two-phase (preview, then confirm=true):
    - "start": enters the calibration state (only from idle), heats the bed (55 C unless bed_temp)
      and nozzles, homes, then for each head heats, wipes on the cleaner (clean=true) and cools to
      140 C. Takes several minutes; returns at once: follow with wait_until_idle.
    - "probe": after start has finished AND the build plate has been REMOVED (the probe touches the
      bed's reference point; the firmware checks the plate is off), probes each head, saves the
      offsets and leaves the calibration state. Follow with wait_until_idle, then get_head_offsets.
    - "exit": leave the calibration state and turn heaters off (after an error or to cancel).
    heads: which heads (1-4, default all); probing a subset saves just those. Do not resend a
    phase while it runs; check wait_until_idle / get_console instead."""
    if phase not in ("start", "probe", "exit"):
        return {"error": "phase must be start, probe or exit"}
    hs = offsets.parse_heads(heads)
    if isinstance(hs, str):
        return {"error": hs}
    try:
        async with _client() as c:
            _, st = await _ready_and_status(c)
            q = await c.objects_query_named(offsets.QUERY)
            if "extruder_offset_calibration" not in q:
                return {"error": "not_supported", "reason": "no extruder_offset_calibration object (not a Snapmaker U1?)"}
            state = offsets.summarize(q)
            ms = state["main_state"]
            if phase == "start":
                if ms != "IDLE" or st["job"]["state"] in ("printing", "paused"):
                    return {"error": "not_idle", "main_state": ms, "job_state": st["job"]["state"],
                            "reason": "the calibration can only start from idle; finish or cancel the print "
                                      "(or calibrate_head_offsets(phase='exit') if a calibration is stuck)"}
                script = offsets.start_script(hs, clean, bed_temp)
                note = ("next: wait_until_idle (several minutes), then take the build plate OFF, then "
                        "calibrate_head_offsets(phase='probe')")
            elif phase == "probe":
                if ms != "XYZ_OFFSET_CALIBRATE":
                    return {"error": "not_started", "main_state": ms,
                            "reason": "run calibrate_head_offsets(phase='start') first and wait for it to finish"}
                script = offsets.probe_script(hs)
                note = "the build plate must be OFF. Next: wait_until_idle, then get_head_offsets; put the plate back"
            else:
                script = ("EXTRUDER_OFFSET_ACTION_EXIT" if ms == "XYZ_OFFSET_CALIBRATE"
                          else "TURN_OFF_HEATERS")
                note = "leaves the calibration state; heaters off"
            preview = {"phase": phase, "heads": hs, "gcode": script, "main_state": ms,
                       "current_offsets": state["heads"], "note": note}
            if not confirm:
                return gate.confirm_required("calibrate_head_offsets", preview)
            try:
                await c.gcode_script(script, reply_timeout=2.0)
                status = "done"
            except NoReply:
                status = "running"
        return {"ok": True, "action": "calibrate_head_offsets", "phase": phase, "status": status,
                "sent": script, "next": note,
                "hint": "sent once; poll wait_until_idle (and get_console for errors), do not resend"}
    except (PrinterNotReady, ApiError) as e:
        return _err(e)


@mcp.tool()
async def set_temperature(heater: str, target: float, confirm: bool = False) -> dict:
    """Set a heater target. heater: 'extruder' (tool 1), 'extruder1'..'extruder3' (tools 2-4 on a
    toolchanger), or 'bed'. Hard ceilings: any extruder 300C, bed 110C.
    Refused unless Klipper is ready. TWO-PHASE: without confirm=true this returns a preview and
    changes nothing; ask the user, then call again with confirm=true."""
    try:
        h = _HEATERS.get(heater)
        if not h:
            return {"error": f"unknown heater '{heater}' (use 'extruder', 'extruder1'..'extruder3' or 'bed')"}
        is_ex = h.startswith("extruder")
        gate.check_bounds(**({"extruder_c": target} if is_ex else {"bed_c": target}))
        gcode = f"SET_HEATER_TEMPERATURE HEATER={h} TARGET={float(target):g}"
        async with _client() as c:
            _, st = await _ready_and_status(c)
            cur = (st["temps"].get(h) or {}).get("actual") if is_ex else st["temps"]["bed"]["actual"]
            if not confirm:
                return gate.confirm_required("set_temperature", {"heater": h, "target_c": target,
                                                                 "current_c": cur, "gcode": gcode})
            await c.gcode_script(gcode)
        return {"ok": True, "action": "set_temperature", "heater": h, "target_c": target}
    except (Refused, PrinterNotReady, ApiError) as e:
        return _err(e)


_JOB_METHODS = {"pause_print": "print_pause", "resume_print": "print_resume", "cancel_print": "print_cancel"}


async def _job_control(action: str, confirm: bool) -> dict:
    try:
        async with _client() as c:
            _, st = await _ready_and_status(c)
            if not confirm:
                preview = _job_preview(st)
                if action == "resume_print" and st["job"]["state"] == "paused":
                    # Tracker #50: after a runout with no backup, RESUME keeps failing until the
                    # empty head is loaded; say which one before trying.
                    try:
                        ro = await _runout_state(c)
                        if ro.get("supported") and ro.get("empty_used_heads"):
                            preview["resume_needs_filament_in_heads"] = ro["empty_used_heads"]
                            preview["note"] = ("a head this job uses has no filament; load it first "
                                               "or RESUME fails with a filament runout error")
                    except Exception:  # best effort
                        pass
                return gate.confirm_required(action, preview)
            await getattr(c, _JOB_METHODS[action])()
        return {"ok": True, "action": action, "was": _job_preview(st)}
    except (PrinterNotReady, ApiError) as e:
        return _err(e)


@mcp.tool()
async def pause_print(confirm: bool = False) -> dict:
    """Pause the running print. TWO-PHASE: without confirm=true this returns a preview and changes
    nothing; ask the user, then call again with confirm=true."""
    return await _job_control("pause_print", confirm)


@mcp.tool()
async def resume_print(confirm: bool = False) -> dict:
    """Resume a paused print. TWO-PHASE: without confirm=true this returns a preview and changes
    nothing; ask the user, then call again with confirm=true."""
    return await _job_control("resume_print", confirm)


@mcp.tool()
async def cancel_print(confirm: bool = False) -> dict:
    """Cancel the running print (irreversible). TWO-PHASE: without confirm=true this returns a
    preview and changes nothing; ask the user, then call again with confirm=true."""
    return await _job_control("cancel_print", confirm)


@mcp.tool()
async def tune_live(pressure_advance: float | None = None, z_offset: float | None = None,
                    flow_percent: float | None = None, fan_percent: float | None = None,
                    confirm: bool = False) -> dict:
    """Live-tune the running (or next) print: pressure_advance (0..1), z_offset in mm (-2..2, absolute
    baby-step offset), flow_percent (50..150), fan_percent (0..100). Sends each provided value as its
    own gcode line, in sequence, under a single confirmation. Refused unless Klipper is ready.
    TWO-PHASE: without confirm=true this returns a preview (the exact gcode lines and current values)
    and changes nothing; ask the user, then call again with confirm=true."""
    try:
        lines = gate.tune_gcode(pressure_advance, z_offset, flow_percent, fan_percent)
        if not lines:
            return {"error": "nothing_to_tune"}
        gate.check_bounds(pressure_advance=pressure_advance, z_offset=z_offset,
                          flow_percent=flow_percent, fan_percent=fan_percent)
        async with _client() as c:
            _, st = await _ready_and_status(c)
            current = {"pressure_advance": st["pressure_advance"], "z_offset": st["z_offset"],
                       "flow_factor": st["flow_factor"], "fan_percent": st["fan_percent"]}
            if not confirm:
                return gate.confirm_required("tune_live", {"gcode": lines, "current": current})
            for i, line in enumerate(lines):
                try:
                    await c.gcode_script(line)
                except ApiError as e:
                    return {"error": str(e), "sent": lines[:i], "not_sent": lines[i:]}
        return {"ok": True, "action": "tune_live", "sent": lines}
    except (Refused, PrinterNotReady, ApiError) as e:
        return _err(e)


@mcp.tool()
async def send_gcode(script: str, confirm: bool = False, wait: bool = True) -> dict:
    """Send raw G-code / a Klipper macro, VERBATIM: no rewriting. Multiple lines are allowed and all
    execute under the single confirmation. The numeric ceilings enforced by set_temperature/tune_live
    do NOT apply on this path; the preview shows the exact bytes that will be sent, so review it
    carefully. The general actuator behind the specific tools; prefer those. Refused unless Klipper
    is ready. TWO-PHASE: without confirm=true this returns a preview and changes nothing; ask the
    user, then call again with confirm=true.
    Sent ONCE, never retried. wait=true (default) waits for Klipper to finish (up to the server's
    request timeout); wait=false returns as soon as the command is accepted - use it for long
    macros (homing, offset/probe calibrations, bed mesh) and follow with wait_until_idle.
    status "running" / "sent_no_reply" means the command WAS sent and may still be running:
    do NOT send it again; check wait_until_idle / get_console instead."""
    try:
        script = script.strip()
        if not script:
            return {"error": "empty_script"}
        async with _client() as c:
            _, st = await _ready_and_status(c)
            if not confirm:
                return gate.confirm_required("send_gcode", {"gcode": script, "wait": wait, **_job_preview(st)})
            try:
                await c.gcode_script(script, reply_timeout=None if wait else 2.0)
            except NoReply:
                if not wait:
                    return {"ok": True, "action": "send_gcode", "sent": script, "status": "running",
                            "hint": "sent once and running; poll wait_until_idle, do not resend"}
                return {"ok": False, "action": "send_gcode", "sent": script, "status": "sent_no_reply",
                        "hint": "sent once; no reply before the timeout, so it is probably still running. "
                                "Do NOT resend; use wait_until_idle or get_console."}
        return {"ok": True, "action": "send_gcode", "sent": script, "status": "done"}
    except (PrinterNotReady, ApiError) as e:
        return _err(e)


@mcp.tool()
async def wait_until_idle(timeout_s: float = 120, poll_s: float = 2) -> dict:
    """Wait until Klipper has finished the command or macro it is running (e.g. after
    send_gcode(wait=false) for homing or a calibration). Read-only. Returns idle=true when Klipper's
    idle_timeout leaves "Printing" (or a print job is running/paused, which counts as done with the
    command queue), or idle=false after timeout_s (max 600) - then call it again rather than resending
    the command. Also returns the last console lines so errors are visible."""
    import asyncio, time
    timeout_s = max(1.0, min(float(timeout_s), 600.0))
    poll_s = max(0.5, min(float(poll_s), 30.0))
    deadline = time.monotonic() + timeout_s
    try:
        async with _client() as c:
            while True:
                q = await c.objects_query(["idle_timeout", "print_stats", "webhooks"])
                idle_state = (q.get("idle_timeout") or {}).get("state")
                job = (q.get("print_stats") or {}).get("state")
                klippy = (q.get("webhooks") or {}).get("state")
                busy = idle_state == "Printing" and job not in ("printing", "paused")
                if not busy or klippy not in (None, "ready") or time.monotonic() >= deadline:
                    tail = []
                    try:
                        tail = [g.get("message") for g in (await c.gcode_store(10)) if g.get("message")]
                    except ApiError:
                        pass
                    return {"idle": not busy, "idle_timeout_state": idle_state, "job_state": job,
                            "klippy_state": klippy, "console_tail": tail}
                await asyncio.sleep(poll_s)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def firmware_restart(confirm: bool = False) -> dict:
    """FIRMWARE_RESTART: recover Klipper from a shutdown state (e.g. after power-cycling the printer).
    Does NOT require Klipper to be ready (it is the fix for not-ready). TWO-PHASE: without confirm=true
    this returns a preview (the current state message) and changes nothing; ask the user, then call
    again with confirm=true."""
    try:
        async with _client() as c:
            info = await c.printer_info()
            if not confirm:
                return gate.confirm_required("firmware_restart", {
                    "klippy_state": info.get("state"), "klippy_message": (info.get("state_message") or "").strip()})
            await c.firmware_restart()
        return {"ok": True, "action": "firmware_restart", "was_state": info.get("state")}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def emergency_stop() -> dict:
    """EMERGENCY STOP: immediately halts the printer (Klipper enters shutdown; heaters and motors off).
    Acts at once, no preview, because a delay would defeat its purpose. Recover with firmware_restart."""
    try:
        async with _client() as c:
            await c.emergency_stop()
        return {"ok": True, "action": "emergency_stop"}
    except ApiError as e:
        return _err(e)


async def _apply_print_preferences(c, bed_level: bool, flow_calibrate: bool, timelapse: bool) -> bool:
    """Snapmaker firmware reads these before PRINT_START. Returns False (and starts anyway) when the
    printer does not know SET_PRINT_PREFERENCES, i.e. it is not a Snapmaker U1."""
    b = lambda v: 1 if v else 0
    try:
        await c.gcode_script(f"SET_PRINT_PREFERENCES BED_LEVEL={b(bed_level)} "
                             f"FLOW_CALIBRATE={b(flow_calibrate)} TIME_LAPSE_CAMERA={b(timelapse)}")
        return True
    except ApiError:
        return False


async def _start_and_confirm(c, filename: str) -> dict:
    """POST print/start once. On a lost reply, read print_stats to report what actually happened."""
    import asyncio
    try:
        await c.print_start(filename)
        return {"state": "started"}
    except NoReply:
        for _ in range(5):
            await asyncio.sleep(2)
            try:
                ps = (await c.objects_query(["print_stats"])).get("print_stats") or {}
            except ApiError:
                continue
            if ps.get("state") in ("printing", "paused") and Path(ps.get("filename") or "").name == Path(filename).name:
                return {"state": "started", "note": "no reply to the start request, but the printer is printing it"}
        return {"error": "start_unconfirmed",
                "detail": "start request sent but no reply and the printer is not (yet) printing this "
                          "file; check get_printer_status before trying again"}
    except ApiError as e:
        return {"error": str(e)}


@mcp.tool()
async def start_print(path: str, confirm: bool = False, bed_level: bool = True,
                      flow_calibrate: bool = False, timelapse: bool = False) -> dict:
    """Start a print. path = a LOCAL gcode file on this server (e.g. one saved by orcaslicer-mcp's
    save_gcode; it is uploaded to the printer under its basename) OR a filename already staged on
    the printer, including in a subdirectory (see list_gcode_files; no upload). A bare name that
    also exists as a local file in this server's working directory is treated as local and
    uploaded; the preview's upload_needed / overwrites_existing fields show which. TWO-PHASE:
    without confirm=true this returns a preview and changes nothing; ask the user, then call
    again with confirm=true. Refused unless Klipper is ready and idle.
    Snapmaker U1 print preferences (sent as SET_PRINT_PREFERENCES before starting, the same way
    Snapmaker Orca does): bed_level (default true; without it the start G-code skips the bed mesh
    and reuses the last saved one), flow_calibrate (default false), timelapse (default false).
    On printers without that command the preferences are skipped and the print still starts."""
    try:
        local = Path(path)
        is_local = local.exists() and local.is_file()
        filename = local.name if is_local else Path(path).name
        if is_local and local.suffix.lower() not in _GCODE_SUFFIXES:
            return {"error": "not_gcode", "path": path}
        async with _client() as c:
            _, st = await _ready_and_status(c)
            if st["job"]["state"] in ("printing", "paused"):
                return {"error": "printer_busy", "current": _job_preview(st)}
            on_pi = {f.get("path") for f in await c.files_list("gcodes")}
            if not is_local:
                if path not in on_pi:
                    return {"error": "file_not_found", "path": path,
                            "hint": "not a local file and not on the printer; see list_gcode_files"}
                filename = path
            overwrites_existing = is_local and filename in on_pi
            preview = {"filename": filename, "upload_needed": is_local,
                       "overwrites_existing": overwrites_existing,
                       "size_bytes": local.stat().st_size if is_local else None,
                       "klippy_state": st["klippy"]["state"], "job_state": st["job"]["state"],
                       "print_preferences": {"bed_level": bed_level, "flow_calibrate": flow_calibrate,
                                             "timelapse": timelapse}}
            try:
                ro = await _runout_state(c, None if is_local else filename,
                                         _local_gcode_tools(local) if is_local else None)
                if ro.get("supported"):
                    preview["runout_backups"] = {"heads": ro["heads"], "warnings": ro["warnings"]}
            except Exception:  # best effort: never block a start on this
                pass
            if not confirm:
                return gate.confirm_required("start_print", preview)
            prefs_applied = await _apply_print_preferences(c, bed_level, flow_calibrate, timelapse)
            if is_local:
                try:
                    await c.upload_gcode(str(local), filename)
                except NoReply:
                    # The upload went out but the reply didn't come back: check what is really there
                    # before calling it a failure, and never upload twice.
                    meta = await c.file_metadata(filename)
                    size = local.stat().st_size
                    if not meta or (meta.get("size") not in (None, size)):
                        return {"error": "upload_unconfirmed", "filename": filename,
                                "detail": "upload sent but no reply, and the file on the printer is "
                                          "missing or a different size; check list_gcode_files"}
            started = await _start_and_confirm(c, filename)
            if "error" in started:
                return {**started, "uploaded": is_local, "filename": filename}
        return {"ok": True, "action": "start_print", "filename": filename, "uploaded": is_local,
                "overwrites_existing": overwrites_existing, "print_preferences_applied": prefs_applied}
    except (PrinterNotReady, ApiError) as e:
        return _err(e)


@mcp.tool()
def record_verdict(verdict: str, gcode_filename: str | None = None) -> dict:
    """Attach your quality judgement to a finished print in the shared outcome memory, e.g. 'warped',
    'stringing', 'clean', 'layer shift at 40%'. Defaults to the most recently finished print;
    pass gcode_filename to target another. This is what lets the slicer learn from real results."""
    return outcomes.set_verdict(verdict, gcode_filename)


_TOOL_ANNOTATIONS: dict[str, tuple[str, bool, bool]] = {
    # name: (title, read_only, destructive)
    "get_printer_status": ("Get live printer status", True, False),
    "get_printer_info": ("Get Klipper host info", True, False),
    "get_printer_sensors": ("Get all sensors, fans and outputs", True, False),
    "query_objects": ("Query raw Klipper objects", True, False),
    "list_config_files": ("List printer config files", True, False),
    "get_job_filament": ("Filament used per head by a job", True, False),
    "list_timelapses": ("List timelapse videos", True, False),
    "save_timelapse": ("Save a timelapse to this PC", True, False),
    "wait_for_height": ("Wait for the print to reach a height", True, False),
    "read_config_file": ("Read a printer config file", True, False),
    "write_config_file": ("Write a printer config file (with backup)", False, True),
    "list_print_history": ("List print history", True, False),
    "list_gcode_files": ("List G-code files on printer", True, False),
    "check_runout_backups": ("Check runout backup heads", True, False),
    "get_loaded_filaments": ("Show each head's filament (HelixScreen + printer)", True, False),
    "list_spools": ("List spools and stock", False, False),
    "save_spool": ("Add or edit a spool", False, False),
    "weigh_spool": ("Record a spool weigh-in", False, False),
    "rename_spool": ("Rename a spool", False, False),
    "delete_spool": ("Delete a spool", False, True),
    "load_spool": ("Record a spool loaded on or removed from a head", False, False),
    "import_spools": ("Import the spool spreadsheet into the printer", False, True),
    "export_spools": ("Back up the spool inventory to a spreadsheet", False, False),
    "sync_firmware_filaments": ("Write HelixScreen's filaments to the printer's record", False, True),
    "get_head_offsets": ("Get per-head XYZ offsets", True, False),
    "calibrate_head_offsets": ("Run the head offset calibration", False, True),
    "get_console": ("Get recent console lines", True, False),
    "get_power_devices": ("Get power device states", True, False),
    "get_bed_mesh": ("Get bed mesh", True, False),
    "get_camera_snapshot": ("Take a camera snapshot", True, False),
    "set_temperature": ("Set heater temperature", False, True),
    "pause_print": ("Pause print", False, True),
    "resume_print": ("Resume print", False, True),
    "cancel_print": ("Cancel print", False, True),
    "tune_live": ("Live-tune PA / Z / flow / fan", False, True),
    "send_gcode": ("Send raw G-code", False, True),
    "wait_until_idle": ("Wait until Klipper is idle", True, False),
    "firmware_restart": ("Firmware restart", False, True),
    "emergency_stop": ("Emergency stop", False, True),
    "start_print": ("Start print (upload + start)", False, True),
    "record_verdict": ("Record print verdict", False, False),
}


def _apply_annotations() -> None:
    for name, tool in mcp._tool_manager._tools.items():
        title, read_only, destructive = _TOOL_ANNOTATIONS[name]
        tool.annotations = ToolAnnotations(title=title, readOnlyHint=read_only,
                                           destructiveHint=None if read_only else destructive)


_apply_annotations()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
