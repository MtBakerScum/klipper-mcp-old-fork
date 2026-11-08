"""Async Moonraker REST client. Mirrors orcaslicer-mcp's OrcaClient shape."""
from __future__ import annotations
import asyncio
from pathlib import Path
import httpx
from .config import Config
from .errors import ApiError, NoReply, NotReachable, error_from_status


# Process-wide memory of which URL last answered, keyed by the configured primary URL.
# Every tool call builds a fresh client, so without this each call would re-pay the mDNS
# failure (connect timeout) before reaching the fallback IP. Cleared when the remembered
# URL stops answering, so a printer that moves back to its mDNS name is re-learned.
_LEARNED: dict[str, str] = {}


class MoonrakerClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._base = _LEARNED.get(cfg.base_url, cfg.base_url)
        self._tried: set[str] = set()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout, connect=cfg.connect_timeout))

    async def __aenter__(self) -> "MoonrakerClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    @property
    def base_url(self) -> str:
        return self._base

    def _candidates(self) -> list[str]:
        return [u for u in (self._cfg.base_url, self._cfg.fallback_url) if u]

    def _next_base(self) -> str | None:
        """The next configured URL this client has not yet tried, or None when all are spent."""
        self._tried.add(self._base)
        for u in self._candidates():
            if u not in self._tried:
                return u
        return None

    async def _request(self, method: str, path: str, *, params=None, files=None, data=None, json=None, timeout=None):
        kw = {} if timeout is None else {"timeout": timeout}
        try:
            resp = await self._http.request(method, self._base + path, params=params, files=files, data=data, json=json, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # mDNS can fail while the wired IP still answers (or the reverse): try each configured
            # URL once per client, remember the one that answers, forget it if nothing does.
            nxt = self._next_base()
            if nxt is None:
                _LEARNED.pop(self._cfg.base_url, None)
                raise NotReachable(f"Moonraker not reachable at {self._base}: {e}") from e
            self._base = nxt
            return await self._request(method, path, params=params, files=files, data=data, json=json, timeout=timeout)
        except httpx.TransportError as e:
            # Connected and sent, but the reply never came (read timeout, dropped connection).
            # The printer may be running the request right now: never retry it, on this URL or
            # the fallback, or a long macro runs twice.
            _LEARNED[self._cfg.base_url] = self._base
            raise NoReply(f"sent to Moonraker at {self._base}, but no reply ({type(e).__name__}); "
                          f"it may still be running - check before resending") from e
        _LEARNED[self._cfg.base_url] = self._base
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            raise error_from_status(resp.status_code, body)
        return body.get("result") if isinstance(body, dict) else body

    # --- read ---
    async def server_info(self) -> dict:
        return await self._request("GET", "/server/info")

    async def printer_info(self) -> dict:
        return await self._request("GET", "/printer/info")

    async def objects_query(self, objects: list[str]) -> dict:
        # Moonraker takes each object as a bare query key: ?extruder&heater_bed
        # objects must be single bare tokens (e.g. "extruder", "heater_bed") -- no
        # URL-encoding is applied here, so a name needing escaping would break the query.
        params = "&".join(objects)
        res = await self._request("GET", f"/printer/objects/query?{params}")
        return (res or {}).get("status", {})

    async def objects_list(self) -> list[str]:
        res = await self._request("GET", "/printer/objects/list")
        return (res or {}).get("objects", [])

    async def objects_query_named(self, objects: list[str]) -> dict:
        """Like objects_query, but URL-encodes each name, so 'temperature_sensor cavity' works."""
        from urllib.parse import quote
        params = "&".join(quote(o, safe="") for o in objects)
        res = await self._request("GET", f"/printer/objects/query?{params}")
        return (res or {}).get("status", {})

    async def database_namespace(self, namespace: str) -> dict | None:
        """All keys of a Moonraker database namespace, or None if it doesn't exist."""
        from .errors import NotFound
        try:
            r = await self._request("GET", "/server/database/item", params={"namespace": namespace})
        except NotFound:
            return None
        v = (r or {}).get("value") if isinstance(r, dict) else None
        return v if isinstance(v, dict) else None

    async def database_put(self, namespace: str, key: str, value) -> None:
        await self._request("POST", "/server/database/item", json={"namespace": namespace, "key": key, "value": value})

    async def database_delete(self, namespace: str, key: str) -> None:
        await self._request("DELETE", "/server/database/item", params={"namespace": namespace, "key": key})

    async def database_backup(self, filename: str) -> dict | None:
        """Moonraker's own database backup (newer Moonraker only); None if unsupported."""
        try:
            return await self._request("POST", "/server/database/backup", params={"filename": filename})
        except ApiError:
            return None

    async def history_list(self, limit: int = 20, since: float | None = None) -> list[dict]:
        params = {"limit": limit, "order": "desc"}
        if since is not None:
            params["since"] = since
        res = await self._request("GET", "/server/history/list", params=params)
        return (res or {}).get("jobs", [])

    async def files_list(self, root: str = "gcodes") -> list[dict]:
        return await self._request("GET", "/server/files/list", params={"root": root}) or []

    # --- write ---
    async def upload_gcode(self, local_path: str, filename: str) -> dict:
        # httpx streams a file object synchronously while encoding the multipart body, which
        # would block the event loop for a large gcode; read it on a worker thread instead.
        content = await asyncio.to_thread(Path(local_path).read_bytes)
        return await self._request("POST", "/server/files/upload",
                                   files={"file": (filename, content, "application/octet-stream")},
                                   data={"root": "gcodes"})

    async def print_start(self, filename: str) -> str:
        return await self._request("POST", "/printer/print/start", params={"filename": filename})

    async def print_pause(self) -> str:
        return await self._request("POST", "/printer/print/pause")

    async def print_resume(self) -> str:
        return await self._request("POST", "/printer/print/resume")

    async def print_cancel(self) -> str:
        return await self._request("POST", "/printer/print/cancel")

    async def gcode_script(self, script: str, reply_timeout: float | None = None) -> str:
        """Moonraker answers only when the script has finished. reply_timeout overrides the read
        timeout for this call (connect timeout unchanged); a NoReply means it was sent."""
        t = None
        if reply_timeout is not None:
            t = httpx.Timeout(reply_timeout, connect=self._cfg.connect_timeout)
        return await self._request("POST", "/printer/gcode/script", params={"script": script}, timeout=t)

    async def file_metadata(self, filename: str) -> dict | None:
        try:
            return await self._request("GET", "/server/files/metadata", params={"filename": filename})
        except ApiError:
            return None

    async def emergency_stop(self) -> str:
        return await self._request("POST", "/printer/emergency_stop")

    async def firmware_restart(self) -> str:
        return await self._request("POST", "/printer/firmware_restart")

    async def history_job(self, uid: str) -> dict:
        res = await self._request("GET", "/server/history/job", params={"uid": uid})
        return (res or {}).get("job", {})

    async def file_roots(self) -> list[dict]:
        return await self._request("GET", "/server/files/roots") or []

    async def download(self, root: str, path: str, timeout: float = 120.0) -> bytes:
        from urllib.parse import quote
        url = f"{self._base}/server/files/{root}/{quote(path)}"
        try:
            resp = await self._http.get(url, timeout=httpx.Timeout(timeout, connect=self._cfg.connect_timeout))
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise NotReachable(f"Moonraker not reachable at {self._base}: {e}") from e
        except httpx.TransportError as e:
            raise NoReply(f"no reply downloading {path}: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise error_from_status(resp.status_code, {})
        return resp.content

    # --- config files (root "config") ---
    async def config_files(self) -> list[dict]:
        return await self._request("GET", "/server/files/list", params={"root": "config"}) or []

    async def config_read(self, path: str) -> str:
        from urllib.parse import quote
        try:
            resp = await self._http.get(f"{self._base}/server/files/config/{quote(path)}")
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise NotReachable(f"Moonraker not reachable at {self._base}: {e}") from e
        except httpx.TransportError as e:
            raise NoReply(f"no reply reading {path}: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise error_from_status(resp.status_code, {})
        return resp.text

    async def config_copy(self, src: str, dest: str) -> dict:
        return await self._request("POST", "/server/files/copy",
                                   data={"source": f"config/{src}", "dest": f"config/{dest}"})

    async def config_write(self, path: str, text: str) -> dict:
        from pathlib import PurePosixPath
        p = PurePosixPath(path)
        data = {"root": "config"}
        if str(p.parent) not in ("", "."):
            data["path"] = str(p.parent)
        return await self._request("POST", "/server/files/upload",
                                   files={"file": (p.name, text.encode("utf-8"), "text/plain")}, data=data)

    # --- read-only extras ---
    async def power_devices(self) -> list[dict]:
        res = await self._request("GET", "/machine/device_power/devices")
        return (res or {}).get("devices", [])

    async def webcams(self) -> list[dict]:
        res = await self._request("GET", "/server/webcams/list")
        return (res or {}).get("webcams", [])

    def web_root(self) -> str:
        """The printer's web front end (Fluidd/nginx on port 80): Moonraker's URL without its port.
        Webcam snapshot URLs in Moonraker's list are relative to this."""
        u = httpx.URL(self._base)
        return f"{u.scheme}://{u.host}"

    async def get_bytes(self, url: str) -> tuple[bytes, str]:
        """GET an absolute URL; returns (body, content-type). Raises NotReachable / ApiError."""
        try:
            resp = await self._http.get(url)
        except httpx.TransportError as e:
            raise NotReachable(f"{url} not reachable: {e}") from e
        if resp.status_code >= 400:
            raise error_from_status(resp.status_code, {})
        return resp.content, resp.headers.get("content-type", "")

    async def gcode_store(self, count: int = 100) -> list[dict]:
        res = await self._request("GET", "/server/gcode_store", params={"count": count})
        return (res or {}).get("gcode_store", [])
