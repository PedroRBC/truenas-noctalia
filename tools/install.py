#!/usr/bin/env python3
"""Install the native Noctalia TrueNAS plugin without rewriting user TOML.

The default action is a dry run. Use --apply to register the local source and
apply the one owned bar-widget setting. Noctalia's IPC command persists source
and enabled-plugin state; the settings TOML patch only adds/removes our widget
alias and the corresponding default-bar entry.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Callable

PLUGIN_ID = "PedroRBC/truenas"
SOURCE_NAME = "truenas-local"
WIDGET_ALIAS = "truenas"
WIDGET_TYPE = f"{PLUGIN_ID}:status"
BAR_NAME = "default"

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path.home() / ".config/noctalia/config.toml"
DEFAULT_SETTINGS = Path.home() / ".local/state/noctalia/settings.toml"
DEFAULT_STATE = Path.home() / ".local/state/truenas-noctalia"


class InstallError(RuntimeError):
    """An installation cannot safely proceed without overwriting user state."""


class ConcurrentSettingsChange(InstallError):
    """Settings changed between read and atomic replacement; a safe retry may help."""


def _load_toml(path: Path) -> tuple[str, dict]:
    if not path.exists():
        raise InstallError(f"Noctalia settings file does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"Invalid TOML in {path}: {exc}") from exc
    return text, parsed


def _table_header(text: str, wanted: str) -> tuple[int, int] | None:
    """Return (start, end) offsets for a regular TOML table header."""
    for match in re.finditer(r"(?m)^\s*\[([^\[\]\n]+)\]\s*(?:#.*)?$", text):
        if match.group(1).strip() == wanted:
            return match.start(), match.end()
    return None


def _value_end(text: str, start: int) -> int:
    """Find the end of a TOML value, respecting strings and multiline arrays."""
    quote = None
    escaped = False
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and c == "\\":
                escaped = True
            elif c == quote:
                quote = None
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        elif c == "#" and depth == 0:
            newline = text.find("\n", i)
            return len(text) if newline < 0 else newline
        elif c == "\n" and depth == 0:
            return i
    raise InstallError("Could not find the end of the TOML array")


def _find_bar_end(text: str) -> tuple[int, int]:
    table = _table_header(text, "bar.default")
    if table is None:
        raise InstallError("[bar.default] was not found in active settings")
    # Restrict the search to this table, before the next table header.
    next_header = re.search(r"(?m)^\s*\[\[?[^\n]+\]\]?\s*(?:#.*)?$", text[table[1]:])
    section_end = table[1] + next_header.start() if next_header else len(text)
    section = text[table[1]:section_end]
    match = re.search(r"(?m)^(\s*)end\s*=\s*\[", section)
    if not match:
        raise InstallError("[bar.default].end is missing; refusing to invent bar layout")
    value_start = table[1] + match.end() - 1
    return value_start, _value_end(text, value_start)


def _parse_bar_end(text: str, parsed: dict) -> list[str]:
    try:
        value = parsed["bar"]["default"]["end"]
    except (KeyError, TypeError) as exc:
        raise InstallError("[bar.default].end is missing or is not an array") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InstallError("[bar.default].end must be an array of strings")
    return value


def _append_bar_item(text: str, parsed: dict) -> tuple[str, bool]:
    current = _parse_bar_end(text, parsed)
    if WIDGET_ALIAS in current:
        return text, False
    start, end = _find_bar_end(text)
    array = text[start:end]
    if "\n" in array:
        close = array.rfind("]")
        indent_match = re.search(r"(?m)^(\s*)['\"]", array)
        indent = indent_match.group(1) if indent_match else "        "
        before = array[:close].rstrip()
        comma = "" if before.endswith(",") or before == "[" else ","
        patched_array = before + comma + f"\n{indent}\"{WIDGET_ALIAS}\",\n" + array[close:]
    else:
        close = array.rfind("]")
        before = array[:close].rstrip()
        comma = "" if before.endswith(",") or before == "[" else ","
        patched_array = before + comma + f' "{WIDGET_ALIAS}" ' + array[close:]
    return text[:start] + patched_array + text[end:], True


def _remove_bar_item(text: str, parsed: dict) -> tuple[str, bool]:
    current = _parse_bar_end(text, parsed)
    if WIDGET_ALIAS not in current:
        return text, False
    start, end = _find_bar_end(text)
    array = text[start:end]
    if "\n" in array:
        # The installer adds a standalone quoted entry. Only remove that exact
        # line; if a user has reformatted or annotated it, fail closed.
        pattern = re.compile(rf"(?m)^(\s*)\"{re.escape(WIDGET_ALIAS)}\"\s*,?\s*(?:#.*)?\n")
        patched, count = pattern.subn("", array, count=1)
        if count != 1:
            raise InstallError("The truenas bar entry was edited; refusing to remove it")
        return text[:start] + patched + text[end:], True
    # Inline arrays are rewritten only when removing the exact owned element.
    pieces = re.findall(r"\"(?:\\.|[^\"\\])*\"|'(?:[^']|'')*'", array[1:-1])
    if f'"{WIDGET_ALIAS}"' not in pieces:
        raise InstallError("The truenas bar entry was edited; refusing to remove it")
    kept = [piece for piece in pieces if piece != f'"{WIDGET_ALIAS}"']
    inner = ", ".join(kept)
    return text[:start] + "[" + inner + "]" + text[end:], True


def _append_widget_alias(text: str, parsed: dict) -> tuple[str, bool]:
    widget = parsed.get("widget", {})
    existing = widget.get(WIDGET_ALIAS) if isinstance(widget, dict) else None
    if existing is not None:
        if existing.get("type") != WIDGET_TYPE:
            raise InstallError(f"[widget.{WIDGET_ALIAS}] already exists with a different type")
        return text, False
    separator = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    return text + separator + f"[widget.{WIDGET_ALIAS}]\ntype = {json.dumps(WIDGET_TYPE)}\n", True


def _remove_widget_alias(text: str, parsed: dict) -> tuple[str, bool]:
    widget = parsed.get("widget", {})
    existing = widget.get(WIDGET_ALIAS) if isinstance(widget, dict) else None
    if existing is None:
        return text, False
    if existing != {"type": WIDGET_TYPE}:
        raise InstallError(f"[widget.{WIDGET_ALIAS}] changed since install; refusing to remove it")
    header = re.search(r"(?m)^\s*\[widget\.truenas\]\s*$", text)
    if not header:
        raise InstallError("Could not locate the owned [widget.truenas] table")
    next_header = re.search(r"(?m)^\s*\[\[?[^\n]+\]\]?\s*(?:#.*)?$", text[header.end():])
    end = header.end() + next_header.start() if next_header else len(text)
    start = header.start()
    # Include the table's trailing newline(s), but not unrelated preceding data.
    while end < len(text) and text[end] == "\n":
        end += 1
    return text[:start].rstrip("\n") + "\n\n" + text[end:], True


def _atomic_write(path: Path, original: str, updated: str, state_dir: Path) -> Path | None:
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current != original:
        raise ConcurrentSettingsChange(f"{path} changed during installation; refusing to overwrite concurrent edits")
    if updated == original:
        return None
    backup = None
    if path.exists():
        backup_dir = state_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f%z")
        backup = backup_dir / f"{path.name}.{stamp}.bak"
        backup.write_text(original, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        # Recheck after backup/temp-file I/O, as Noctalia may asynchronously
        # persist a new config while the replacement is being prepared.
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != original:
            raise ConcurrentSettingsChange(f"{path} changed during installation; refusing to overwrite concurrent edits")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def _backup_text(path: Path, text: str, state_dir: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f%z")
    backup = backup_dir / f"{path.name}.{stamp}.bak"
    backup.write_text(text, encoding="utf-8")
    return backup


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _ipc(args: list[str], runner: Callable = subprocess.run) -> str:
    try:
        result = runner(["noctalia", "msg", *args], check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise InstallError("Noctalia CLI is not installed or not in PATH") from exc
    if result.returncode:
        message = (result.stderr or result.stdout or "Noctalia IPC command failed").strip()
        raise InstallError(message)
    return result.stdout.strip()


def _source_rows(output: str) -> dict[str, tuple[str, str]]:
    rows = {}
    for line in output.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3:
            rows[fields[0]] = (fields[1], fields[2])
    return rows


def _enabled(parsed: dict) -> list[str]:
    value = parsed.get("plugins", {}).get("enabled", [])
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise InstallError("[plugins].enabled must be an array of plugin IDs")
    return value


def _plan(settings_path: Path, config_path: Path) -> dict:
    settings_text, settings = _load_toml(settings_path)
    # Read/validate the base config too, but never rewrite it: plugin sources
    # are managed through Noctalia's supported IPC source command.
    _load_toml(config_path)
    current = settings.get("widget", {}).get(WIDGET_ALIAS)
    if current is not None and current.get("type") != WIDGET_TYPE:
        raise InstallError(f"[widget.{WIDGET_ALIAS}] already exists with a different type")
    end = _parse_bar_end(settings_text, settings)
    return {
        "settings_text": settings_text,
        "settings": settings,
        "source": {"name": SOURCE_NAME, "kind": "path", "location": str(REPO_ROOT)},
        "source_command": ["noctalia", "msg", "plugins", "source", "add", SOURCE_NAME, "path", str(REPO_ROOT)],
        "enable_command": ["noctalia", "msg", "plugins", "enable", PLUGIN_ID],
        "bar_entry_added": WIDGET_ALIAS not in end,
        "widget_alias_added": current is None,
        "enabled_before": PLUGIN_ID in _enabled(settings),
    }


def install(args: argparse.Namespace) -> int:
    settings_path, config_path, state_dir = args.settings, args.config, args.state_dir
    if not args.no_ipc and (settings_path != DEFAULT_SETTINGS or config_path != DEFAULT_CONFIG):
        raise InstallError("Refusing Noctalia IPC with fixture/custom config paths; pass --no-ipc")
    plan = _plan(settings_path, config_path)
    print(f"Mode: {'apply' if args.apply else 'dry run'}")
    print(f"Plugin: {PLUGIN_ID}")
    print(f"Local source: {SOURCE_NAME} -> {REPO_ROOT}")
    print(f"Widget: {WIDGET_ALIAS} ({WIDGET_TYPE})")
    print(f"Default bar end entry: {'add' if plan['bar_entry_added'] else 'already present'}")
    if not args.apply:
        print("No files or Noctalia settings were changed. Re-run with --apply to install.")
        return 0

    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = state_dir / "install.json"
    previous_manifest = {}
    if manifest_path.exists():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"Could not read installer ownership record: {exc}") from exc
        if previous_manifest.get("plugin_id") != PLUGIN_ID or previous_manifest.get("settings_path") != str(settings_path):
            raise InstallError("Existing installer ownership record does not match this plugin/settings path")

    # Preserve the exact pre-IPC settings before Noctalia can persist source and
    # enabled-plugin changes. The subsequent widget patch always uses a fresh read.
    original_settings = plan["settings_text"]
    initial_backup = previous_manifest.get("backup") or _backup_text(settings_path, original_settings, state_dir)
    manifest = {
        "plugin_id": PLUGIN_ID,
        "source_name": SOURCE_NAME,
        "source_location": str(REPO_ROOT),
        "source_added": bool(previous_manifest.get("source_added", False)),
        "plugin_enabled_by_installer": bool(previous_manifest.get("plugin_enabled_by_installer", False)),
        "widget_alias_added": bool(previous_manifest.get("widget_alias_added", plan["widget_alias_added"])),
        "bar_entry_added": bool(previous_manifest.get("bar_entry_added", plan["bar_entry_added"])),
        "settings_path": str(settings_path),
        "backup": str(initial_backup) if initial_backup else None,
        "no_ipc": args.no_ipc,
    }
    _write_manifest(manifest_path, manifest)

    source_rows = _source_rows(_ipc(["plugins", "source", "list"])) if not args.no_ipc else {}
    existing_source = source_rows.get(SOURCE_NAME)
    wanted_source = ("path", str(REPO_ROOT))
    if existing_source and existing_source != wanted_source:
        raise InstallError(f"Source name {SOURCE_NAME!r} already points to {existing_source[1]}")

    if not args.no_ipc:
        if not existing_source:
            _ipc(["plugins", "source", "add", SOURCE_NAME, "path", str(REPO_ROOT)])
            manifest["source_added"] = True
            _write_manifest(manifest_path, manifest)
        plugin_rows = _ipc(["plugins", "list"])
        plugin_is_enabled = any(
            line.split()[0] == PLUGIN_ID and "enabled" in line.split()[1:]
            for line in plugin_rows.splitlines() if line.split()
        )
        if not plugin_is_enabled:
            _ipc(["plugins", "enable", PLUGIN_ID])
            if not plan["enabled_before"]:
                manifest["plugin_enabled_by_installer"] = True
                _write_manifest(manifest_path, manifest)

    # Persist prospective ownership before retrying, then keep the critical
    # read/patch/compare/replace window free of other slow ownership writes.
    manifest["widget_alias_added"] = bool(manifest["widget_alias_added"] or plan["widget_alias_added"])
    manifest["bar_entry_added"] = bool(manifest["bar_entry_added"] or plan["bar_entry_added"])
    _write_manifest(manifest_path, manifest)

    backup = None
    for attempt in range(5):
        # IPC and Noctalia's async config writer can race this operation. Each
        # retry starts from fresh content and reapplies only our narrow edits.
        current_text, current_settings = _load_toml(settings_path)
        updated, _ = _append_widget_alias(current_text, current_settings)
        updated, _ = _append_bar_item(updated, tomllib.loads(updated))
        tomllib.loads(updated)
        try:
            backup = _atomic_write(settings_path, current_text, updated, state_dir)
            break
        except ConcurrentSettingsChange:
            if attempt == 4:
                raise
            time.sleep(0.025 * (attempt + 1))
    if backup:
        manifest["post_ipc_backup"] = str(backup)
    _write_manifest(manifest_path, manifest)
    if not args.no_ipc:
        _ipc(["config-reload"])
    print(f"Installed. Settings backup: {initial_backup or 'not needed'}")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    manifest_path = args.state_dir / "install.json"
    if not manifest_path.exists():
        raise InstallError(f"No installer ownership record at {manifest_path}; refusing unsafe uninstall")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"Could not read installer ownership record: {exc}") from exc
    if manifest.get("plugin_id") != PLUGIN_ID or manifest.get("settings_path") != str(args.settings):
        raise InstallError("Installer ownership record does not match this plugin/settings path")
    original, parsed = _load_toml(args.settings)
    # Validate ownership before any IPC mutation. This is a preview only until
    # --apply is supplied, and also fails closed on user-edited widget values.
    preview = original
    if manifest.get("bar_entry_added"):
        preview, _ = _remove_bar_item(preview, parsed)
        parsed = tomllib.loads(preview)
    if manifest.get("widget_alias_added"):
        preview, _ = _remove_widget_alias(preview, parsed)
    tomllib.loads(preview)
    print(f"Mode: {'apply' if args.apply else 'dry run'}")
    print(f"Owned bar entry: {'remove' if manifest.get('bar_entry_added') else 'leave unchanged'}")
    print(f"Owned widget alias: {'remove' if manifest.get('widget_alias_added') else 'leave unchanged'}")
    print(f"Owned plugin enabled state: {'disable' if manifest.get('plugin_enabled_by_installer') else 'leave unchanged'}")
    print(f"Owned plugin source: {'remove' if manifest.get('source_added') else 'leave unchanged'}")
    if not args.apply:
        print("No files or Noctalia settings were changed. Re-run with --uninstall --apply to remove.")
        return 0
    if args.no_ipc and not manifest.get("no_ipc") and (
        manifest.get("source_added") or manifest.get("plugin_enabled_by_installer")
    ):
        raise InstallError("This installation owns live Noctalia resources; rerun without --no-ipc")
    live_ipc = not args.no_ipc and not manifest.get("no_ipc")
    if live_ipc and (args.settings != DEFAULT_SETTINGS or args.config != DEFAULT_CONFIG):
        raise InstallError("Refusing Noctalia IPC with fixture/custom config paths; pass --no-ipc")

    # Keep the pre-IPC settings available for recovery until the last reload.
    pre_ipc_backup = manifest.get("backup") or _backup_text(args.settings, original, args.state_dir)
    manifest["backup"] = str(pre_ipc_backup) if pre_ipc_backup else None
    _write_manifest(manifest_path, manifest)

    source_present = False
    if live_ipc and manifest.get("source_added"):
        source_rows = _source_rows(_ipc(["plugins", "source", "list"]))
        registered = source_rows.get(manifest["source_name"])
        expected = ("path", manifest["source_location"])
        if registered and registered != expected:
            raise InstallError(
                f"Owned source {manifest['source_name']!r} now points to {registered[1]}; refusing to remove it"
            )
        source_present = registered == expected

    # IPC can persist stale in-memory config. Complete owned runtime changes
    # first, then reread disk and make our widget patch the final config write.
    if live_ipc:
        if manifest.get("plugin_enabled_by_installer"):
            _ipc(["plugins", "disable", PLUGIN_ID])
        if source_present:
            _ipc(["plugins", "source", "remove", manifest["source_name"]])

    current_text, current_settings = _load_toml(args.settings)
    updated = current_text
    if manifest.get("bar_entry_added"):
        updated, _ = _remove_bar_item(updated, current_settings)
        current_settings = tomllib.loads(updated)
    if manifest.get("widget_alias_added"):
        updated, _ = _remove_widget_alias(updated, current_settings)
    tomllib.loads(updated)
    settings_backup = _atomic_write(args.settings, current_text, updated, args.state_dir)
    if settings_backup:
        manifest["post_ipc_backup"] = str(settings_backup)
        _write_manifest(manifest_path, manifest)
    if live_ipc:
        _ipc(["config-reload"])
    manifest_path.unlink()
    print(f"Uninstalled owned settings. Settings backup: {pre_ipc_backup or settings_backup or 'not needed'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply the selected action; default is dry-run")
    parser.add_argument("--uninstall", action="store_true", help="preview or remove resources recorded as installer-owned")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="base Noctalia config.toml")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS, help="active Noctalia settings.toml")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE, help="installer ownership record and backups")
    parser.add_argument("--no-ipc", action="store_true", help="patch settings only (for offline tests/fixtures)")
    args = parser.parse_args(argv)
    try:
        return uninstall(args) if args.uninstall else install(args)
    except (InstallError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
