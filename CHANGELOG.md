# Changelog

## [Unreleased]
### Changed
- Requires the mcp SDK 2.x (`mcp>=2.2,<3`): `FastMCP` became `MCPServer`. The server now reports its own package version to clients.
- The Moonraker client remembers which URL (mDNS name or fallback IP) last answered, process-wide, so every tool call no longer re-pays a dead mDNS lookup; the memory is dropped if that URL stops answering and both are retried.
- Separate connect timeout (`MOONRAKER_CONNECT_TIMEOUT`, default 3s) so failover to the IP happens in seconds.
- `start_print` reads the gcode file on a worker thread instead of blocking the event loop while httpx encodes the upload.
- `start_print` sends Snapmaker U1 print preferences (`SET_PRINT_PREFERENCES`: `bed_level` default on, `flow_calibrate` and `timelapse` default off) before starting, so prints started from the MCP run the bed mesh like Snapmaker Orca's send dialog. Printers without that command skip it and start anyway.
- `get_console` drops duplicate entries (same time, type and message) that Moonraker can store several times.
- The capture systemd unit is sandboxed (ProtectSystem=strict, ProtectHome=read-only with the store directory writable, NoNewPrivileges, empty capability set, `@system-service` syscall filter).
### Added
- get_loaded_filaments (tracker #68): each U1 head's filament as set in HelixScreen (Moonraker `lane_data` database: brand, spool name, material, colour, Spoolman spool, grams, temps) beside the printer's own record (`print_task_config`) and the feeder sensor; `stale_heads` lists where they disagree.
- sync_firmware_filaments(heads?, confirm): rewrites the printer's record from HelixScreen the way HelixScreen does (`SET_PRINT_FILAMENT_CONFIG`, vendor Snapmaker/Generic, sub-type or Basic, colour). Refused during a print; skips RFID-tag heads.
- check_runout_backups / start_print preview name heads by their HelixScreen filament and warn when the printer's record (what refill matches on) disagrees.
- get_camera_snapshot: a still from the printer camera as an image (cameras from Moonraker's webcam list, pick one by name; falls back to the usual /webcam snapshot paths). Read-only.
- Read tools: get_printer_status, get_printer_info, list_print_history, list_gcode_files.
- Read tools: get_console (recent console lines, temp reports filtered, optional regex), get_power_devices, get_bed_mesh (grid plus range, tilt and corner summary).
- Two-phase gated control: set_temperature, pause/resume/cancel_print, tune_live, send_gcode, firmware_restart; emergency_stop acts immediately.
- start_print uploads a local gcode to the printer and starts it, after confirmation.
- klipper-mcp-capture service records every finished print into the shared outcome store and backfills from history on reconnect.
- record_verdict attaches a human quality note to a finished print.
