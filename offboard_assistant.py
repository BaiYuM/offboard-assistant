#!/usr/bin/env python3
"""
Privacy-preserving onboarding/offboarding assistant.

The tool records locations and metadata that help clean up work-related
residue later. It deliberately does not decrypt, store, or print passwords,
chat contents, cookies, tokens, or API key values.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import winreg
except ImportError:  # pragma: no cover - only available on Windows
    winreg = None  # type: ignore[assignment]


APP_DIR = ".offboard-assistant"
APP_NAME = "OffboardAssistant"
BASELINE_FILE = "baseline.json"
SNAPSHOT_FILE = "latest-snapshot.json"
REPORT_FILE = "offboarding-report.md"
INSTALL_MONITOR_STATE_FILE = "install-monitor-state.json"
INSTALL_EVENTS_FILE = "install-events.jsonl"

SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials",
    "credentials.json",
    "config.json",
    "settings.json",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
}

SENSITIVE_PATTERNS = {
    "*.pem",
    "*.key",
    "*credentials*",
    "*secret*",
    "*token*",
}

DEFAULT_SCAN_DIR_NAMES = {
    "Desktop",
    "Documents",
    "Downloads",
    "Projects",
    "workspace",
    "source",
    "repos",
}

AI_APP_CONFIG_DIR_NAMES = {
    ".aws",
    ".azure",
    ".claude",
    ".codex",
    ".config",
    ".cursor",
    ".gemini",
    ".openai",
    ".ssh",
}

SECRET_CONTENT_EXTENSIONS = {
    "",
    ".conf",
    ".config",
    ".env",
    ".ini",
    ".json",
    ".jsonc",
    ".properties",
    ".rc",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SECRET_PROVIDER_PATTERNS = [
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Generic bearer token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b")),
    (
        "Generic secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|access[_-]?key|private[_-]?key|password)\b\s*[:=]\s*[\"']?([A-Za-z0-9._~+/=-]{16,})"
        ),
    ),
]

PATH_LIKE_ENV_NAMES = {
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
    "CARGO_HOME",
    "GOPATH",
    "GOROOT",
    "GRADLE_HOME",
    "JAVA_HOME",
    "M2_HOME",
    "MAVEN_HOME",
    "NODE_HOME",
    "NVM_HOME",
    "PATH",
    "PYTHONPATH",
    "RUSTUP_HOME",
}


@dataclass(frozen=True)
class BrowserProfile:
    browser: str
    profile: str
    root: Path
    login_data: Path | None = None
    firefox_logins: Path | None = None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_since(value: str) -> dt.datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed.astimezone(dt.timezone.utc)


def filetime_from_windows_install_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc).isoformat()


def chrome_time_to_iso(value: int | float | None) -> str | None:
    if not value:
        return None
    try:
        epoch = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
        return (epoch + dt.timedelta(microseconds=int(value))).isoformat()
    except (OverflowError, ValueError):
        return None


def unix_ms_to_iso(value: int | float | None) -> str | None:
    if not value:
        return None
    try:
        return dt.datetime.fromtimestamp(float(value) / 1000, tz=dt.timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        return None


def mtime_iso(path: Path) -> str | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat()
    except OSError:
        return None


def safe_rel(path: Path) -> str:
    return str(path.expanduser().resolve())


def stable_id(parts: Iterable[str | None]) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def mask_identifier(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if "@" in value:
        name, domain = value.split("@", 1)
        if len(name) <= 2:
            masked = name[:1] + "*"
        else:
            masked = name[:1] + "***" + name[-1:]
        return f"{masked}@{domain}"
    if len(value) <= 3:
        return value[:1] + "***"
    return value[:2] + "***" + value[-1:]


def mask_secret(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    if len(value) <= 12:
        return value[:2] + "***"
    return value[:6] + "***" + value[-4:]


def secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def ensure_state_dir(base: Path) -> Path:
    state_dir = base / APP_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def default_state_base() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / APP_NAME


def state_dir_from_arg(value: str | None) -> Path:
    if value:
        return ensure_state_dir(Path(value))
    base = default_state_base()
    base.mkdir(parents=True, exist_ok=True)
    state_dir = ensure_state_dir(base)
    migrate_legacy_state_if_needed(state_dir)
    return state_dir


def migrate_legacy_state_if_needed(target_state_dir: Path) -> list[str]:
    legacy_state_dir = Path.cwd() / APP_DIR
    if not legacy_state_dir.exists() or legacy_state_dir.resolve() == target_state_dir.resolve():
        return []
    migrated: list[str] = []
    for filename in (
        BASELINE_FILE,
        SNAPSHOT_FILE,
        INSTALL_MONITOR_STATE_FILE,
        INSTALL_EVENTS_FILE,
        REPORT_FILE,
        "cleanup-actions.md",
        "handled-items.json",
    ):
        source = legacy_state_dir / filename
        target = target_state_dir / filename
        if not source.exists() or target.exists():
            continue
        try:
            shutil.copy2(source, target)
            migrated.append(filename)
        except OSError:
            continue
    return migrated


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def list_registry_values(root: Any, subkey: str) -> dict[str, str]:
    if winreg is None:
        return {}
    values: dict[str, str] = {}
    try:
        with winreg.OpenKey(root, subkey) as key:
            count = winreg.QueryInfoKey(key)[1]
            for index in range(count):
                name, value, _ = winreg.EnumValue(key, index)
                values[name] = str(value)
    except OSError:
        return {}
    return values


def scan_environment() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if winreg is not None:
        locations = [
            ("user", winreg.HKEY_CURRENT_USER, r"Environment"),
            (
                "machine",
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        ]
        for scope, root, key in locations:
            for name in sorted(list_registry_values(root, key)):
                entries.append(
                    {
                        "id": stable_id(["env", scope, name]),
                        "type": "environment_variable",
                        "scope": scope,
                        "name": name,
                        "value_recorded": False,
                    }
                )
    else:
        for name in sorted(os.environ):
            entries.append(
                {
                    "id": stable_id(["env", "process", name]),
                    "type": "environment_variable",
                    "scope": "process",
                    "name": name,
                    "value_recorded": False,
                }
            )
    return entries


def environment_registry_sources() -> list[tuple[str, Any, str]]:
    if winreg is None:
        return []
    return [
        ("user", winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            "machine",
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ]


def scan_environment_for_install_monitor() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if winreg is not None:
        sources = environment_registry_sources()
        for scope, root, key in sources:
            values = list_registry_values(root, key)
            for name, value in sorted(values.items()):
                upper_name = name.upper()
                item: dict[str, Any] = {
                    "id": stable_id(["install-env", scope, upper_name]),
                    "type": "install_environment_variable",
                    "scope": scope,
                    "name": name,
                    "value_recorded": False,
                }
                if upper_name in PATH_LIKE_ENV_NAMES:
                    paths = split_path_like_value(value)
                    item["path_entries"] = paths
                    item["path_entries_recorded"] = True
                rows.append(item)
    else:
        for name, value in sorted(os.environ.items()):
            upper_name = name.upper()
            item = {
                "id": stable_id(["install-env", "process", upper_name]),
                "type": "install_environment_variable",
                "scope": "process",
                "name": name,
                "value_recorded": False,
            }
            if upper_name in PATH_LIKE_ENV_NAMES:
                item["path_entries"] = split_path_like_value(value)
                item["path_entries_recorded"] = True
            rows.append(item)
    return rows


def split_path_like_value(value: str) -> list[str]:
    if not value:
        return []
    separator = ";" if os.name == "nt" else ":"
    entries = []
    for part in value.split(separator):
        part = part.strip().strip('"')
        if part:
            entries.append(part)
    return entries


def scan_installed_apps_from_registry() -> list[dict[str, Any]]:
    if winreg is None:
        return []
    uninstall_paths = [
        ("HKCU", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (
            "HKLM32",
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    ]
    apps: list[dict[str, Any]] = []
    for hive_name, root, subkey in uninstall_paths:
        try:
            with winreg.OpenKey(root, subkey) as key:
                subkey_count = winreg.QueryInfoKey(key)[0]
                for index in range(subkey_count):
                    child_name = winreg.EnumKey(key, index)
                    child_path = f"{subkey}\\{child_name}"
                    values = list_registry_values(root, child_path)
                    display_name = values.get("DisplayName")
                    if not display_name:
                        continue
                    install_location = values.get("InstallLocation") or values.get("InstallSource")
                    install_date = filetime_from_windows_install_date(values.get("InstallDate"))
                    apps.append(
                        {
                            "id": stable_id(["app", hive_name, child_name, display_name]),
                            "type": "installed_app",
                            "name": display_name,
                            "publisher": values.get("Publisher"),
                            "version": values.get("DisplayVersion"),
                            "install_location": install_location,
                            "install_date": install_date,
                            "source": f"{hive_name}\\{child_path}",
                        }
                    )
        except OSError:
            continue
    return apps


def default_install_watch_dirs() -> list[Path]:
    candidates = [
        Path(os.environ.get("ProgramFiles", "")),
        Path(os.environ.get("ProgramFiles(x86)", "")),
        Path(os.environ.get("LOCALAPPDATA", "")),
        Path(os.environ.get("APPDATA", "")),
        Path(os.environ.get("ProgramData", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    unique: dict[str, Path] = {}
    for path in candidates:
        if not str(path) or not path.exists():
            continue
        try:
            unique[safe_rel(path)] = path
        except OSError:
            continue
    return list(unique.values())


def scan_top_level_paths(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for root in paths:
        if not root.exists():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name == APP_DIR:
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            rows.append(
                {
                    "id": stable_id(["install-path", safe_rel(child)]),
                    "type": "install_path_observation",
                    "path": safe_rel(child),
                    "root": safe_rel(root),
                    "name": child.name,
                    "kind": "directory" if child.is_dir() else "file",
                    "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat(),
                    "size": stat.st_size,
                }
            )
    return rows


def install_monitor_snapshot(watch_dirs: list[Path]) -> dict[str, Any]:
    apps = scan_installed_apps_from_registry()
    env = scan_environment_for_install_monitor()
    paths = scan_top_level_paths(watch_dirs)
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "privacy": {
            "passwords_recorded": False,
            "secret_values_recorded": False,
            "chat_contents_recorded": False,
        },
        "watch_dirs": [safe_rel(path) for path in watch_dirs if path.exists()],
        "apps": apps,
        "environment": env,
        "paths": paths,
    }


def index_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in items if item.get("id")}


def changed_path_like_env_entries(
    previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item_id, current_item in current.items():
        previous_item = previous.get(item_id)
        if previous_item is None:
            continue
        before = previous_item.get("path_entries")
        after = current_item.get("path_entries")
        if before is None or after is None or before == after:
            continue
        before_set = set(before)
        after_set = set(after)
        rows.append(
            {
                "id": current_item.get("id"),
                "scope": current_item.get("scope"),
                "name": current_item.get("name"),
                "added_path_entries": sorted(after_set - before_set),
                "removed_path_entries": sorted(before_set - after_set),
                "value_recorded": False,
                "path_entries_recorded": True,
            }
        )
    return rows


def diff_install_monitor_snapshots(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    previous_apps = index_by_id(previous.get("apps", []))
    current_apps = index_by_id(current.get("apps", []))
    previous_env = index_by_id(previous.get("environment", []))
    current_env = index_by_id(current.get("environment", []))
    previous_paths = index_by_id(previous.get("paths", []))
    current_paths = index_by_id(current.get("paths", []))

    changed_paths: list[dict[str, Any]] = []
    for item_id, current_item in current_paths.items():
        previous_item = previous_paths.get(item_id)
        if previous_item is None:
            continue
        if (
            previous_item.get("modified_at") != current_item.get("modified_at")
            or previous_item.get("size") != current_item.get("size")
        ):
            changed_paths.append(current_item)

    return {
        "new_apps": [current_apps[item_id] for item_id in sorted(current_apps.keys() - previous_apps.keys())],
        "removed_apps": [previous_apps[item_id] for item_id in sorted(previous_apps.keys() - current_apps.keys())],
        "new_environment_variables": [
            current_env[item_id] for item_id in sorted(current_env.keys() - previous_env.keys())
        ],
        "removed_environment_variables": [
            previous_env[item_id] for item_id in sorted(previous_env.keys() - current_env.keys())
        ],
        "changed_path_like_environment": changed_path_like_env_entries(previous_env, current_env),
        "new_paths": [current_paths[item_id] for item_id in sorted(current_paths.keys() - previous_paths.keys())],
        "changed_paths": changed_paths,
    }


def install_paths_from_signals(signals: dict[str, list[dict[str, Any]]]) -> list[str]:
    paths: set[str] = set()
    for app in signals.get("new_apps", []):
        location = app.get("install_location")
        if location:
            paths.add(str(location))
    for path_item in signals.get("new_paths", []):
        path = path_item.get("path")
        if path:
            paths.add(str(path))
    for env_item in signals.get("changed_path_like_environment", []):
        for path in env_item.get("added_path_entries", []):
            paths.add(str(path))
    return sorted(paths)


def signals_have_changes(signals: dict[str, list[dict[str, Any]]]) -> bool:
    reliable_signal_names = {
        "new_apps",
        "removed_apps",
        "new_environment_variables",
        "removed_environment_variables",
        "changed_path_like_environment",
        "new_paths",
    }
    return any(bool(signals.get(name)) for name in reliable_signal_names)


def build_install_event(signals: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    install_paths = install_paths_from_signals(signals)
    return {
        "id": stable_id(["install-event", utc_now(), json.dumps(signals, ensure_ascii=False, sort_keys=True)]),
        "type": "install_activity_event",
        "detected_at": utc_now(),
        "signals": signals,
        "install_paths": install_paths,
        "privacy": {
            "passwords_recorded": False,
            "secret_values_recorded": False,
            "chat_contents_recorded": False,
        },
    }


def browser_profile_roots() -> list[BrowserProfile]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming = Path(os.environ.get("APPDATA", ""))
    profiles: list[BrowserProfile] = []

    chromium_roots = [
        ("Chrome", local / "Google" / "Chrome" / "User Data"),
        ("Edge", local / "Microsoft" / "Edge" / "User Data"),
        ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
    ]
    for browser, root in chromium_roots:
        if not root.exists():
            continue
        for profile_dir in root.iterdir():
            login_data = profile_dir / "Login Data"
            if login_data.exists():
                profiles.append(
                    BrowserProfile(
                        browser=browser,
                        profile=profile_dir.name,
                        root=profile_dir,
                        login_data=login_data,
                    )
                )

    firefox_root = roaming / "Mozilla" / "Firefox" / "Profiles"
    if firefox_root.exists():
        for profile_dir in firefox_root.iterdir():
            logins = profile_dir / "logins.json"
            if logins.exists():
                profiles.append(
                    BrowserProfile(
                        browser="Firefox",
                        profile=profile_dir.name,
                        root=profile_dir,
                        firefox_logins=logins,
                    )
                )
    return profiles


def copy_for_read(path: Path, state_dir: Path) -> Path | None:
    tmp = state_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / f"{stable_id([str(path), str(path.stat().st_mtime_ns)])}.sqlite"
    try:
        shutil.copy2(path, target)
    except OSError:
        return None
    return target


def scan_chromium_logins(profile: BrowserProfile, state_dir: Path) -> list[dict[str, Any]]:
    if profile.login_data is None:
        return []
    db_path = copy_for_read(profile.login_data, state_dir)
    if db_path is None:
        return [
            {
                "id": stable_id(["browser-error", profile.browser, profile.profile, str(profile.login_data)]),
                "type": "browser_login_metadata_error",
                "browser": profile.browser,
                "profile": profile.profile,
                "path": safe_rel(profile.login_data),
                "reason": "could_not_copy_locked_database",
            }
        ]
    rows: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT origin_url, action_url, username_value, date_created,
                       date_last_used, date_password_modified
                FROM logins
                """
            )
            for row in cursor.fetchall():
                origin = row["origin_url"] or row["action_url"]
                username_masked = mask_identifier(row["username_value"])
                rows.append(
                    {
                        "id": stable_id(
                            [
                                "browser-login",
                                profile.browser,
                                profile.profile,
                                origin,
                                username_masked,
                            ]
                        ),
                        "type": "browser_login_metadata",
                        "browser": profile.browser,
                        "profile": profile.profile,
                        "origin": origin,
                        "username_masked": username_masked,
                        "created_at": chrome_time_to_iso(row["date_created"]),
                        "last_used_at": chrome_time_to_iso(row["date_last_used"]),
                        "password_modified_at": chrome_time_to_iso(row["date_password_modified"]),
                        "password_recorded": False,
                        "database_path": safe_rel(profile.login_data),
                    }
                )
    except sqlite3.Error as exc:
        rows.append(
            {
                "id": stable_id(["browser-error", profile.browser, profile.profile, str(profile.login_data)]),
                "type": "browser_login_metadata_error",
                "browser": profile.browser,
                "profile": profile.profile,
                "path": safe_rel(profile.login_data),
                "reason": str(exc),
            }
        )
    finally:
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass
    return rows


def scan_firefox_logins(profile: BrowserProfile) -> list[dict[str, Any]]:
    if profile.firefox_logins is None:
        return []
    try:
        data = read_json(profile.firefox_logins)
    except (OSError, json.JSONDecodeError) as exc:
        return [
            {
                "id": stable_id(["browser-error", profile.browser, profile.profile, str(profile.firefox_logins)]),
                "type": "browser_login_metadata_error",
                "browser": profile.browser,
                "profile": profile.profile,
                "path": safe_rel(profile.firefox_logins),
                "reason": str(exc),
            }
        ]
    rows: list[dict[str, Any]] = []
    for item in data.get("logins", []):
        hostname = item.get("hostname")
        rows.append(
            {
                "id": stable_id(
                    [
                        "browser-login",
                        profile.browser,
                        profile.profile,
                        hostname,
                        str(item.get("guid")),
                    ]
                ),
                "type": "browser_login_metadata",
                "browser": profile.browser,
                "profile": profile.profile,
                "origin": hostname,
                "username_masked": None,
                "created_at": unix_ms_to_iso(item.get("timeCreated")),
                "last_used_at": unix_ms_to_iso(item.get("timeLastUsed")),
                "password_modified_at": unix_ms_to_iso(item.get("timePasswordChanged")),
                "password_recorded": False,
                "database_path": safe_rel(profile.firefox_logins),
            }
        )
    return rows


def scan_browser_logins(state_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for profile in browser_profile_roots():
        if profile.login_data:
            rows.extend(scan_chromium_logins(profile, state_dir))
        if profile.firefox_logins:
            rows.extend(scan_firefox_logins(profile))
    return rows


def default_scan_roots() -> list[Path]:
    home = Path.home()
    roots = [Path.cwd()]
    for name in DEFAULT_SCAN_DIR_NAMES:
        candidate = home / name
        if candidate.exists():
            roots.append(candidate)
    for name in AI_APP_CONFIG_DIR_NAMES:
        candidate = home / name
        if candidate.exists():
            roots.append(candidate)
    appdata = Path(os.environ.get("APPDATA", ""))
    localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    for root in (appdata, localappdata):
        if not root.exists():
            continue
        for name in (
            "cc-switch",
            "ccswitch",
            "Claude",
            "Claude Code",
            "Cursor",
            "Code",
            "Windsurf",
            "OpenAI",
            "Anthropic",
        ):
            candidate = root / name
            if candidate.exists():
                roots.append(candidate)
    unique: dict[str, Path] = {}
    for root in roots:
        try:
            unique[safe_rel(root)] = root
        except OSError:
            continue
    return list(unique.values())


def is_sensitive_name(path: Path) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_FILENAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_PATTERNS)


def should_scan_file_contents(path: Path) -> bool:
    if is_sensitive_name(path):
        return True
    if path.suffix.lower() not in SECRET_CONTENT_EXTENSIONS:
        return False
    lowered = safe_rel(path).lower()
    return any(
        marker in lowered
        for marker in (
            "cc-switch",
            "ccswitch",
            "claude",
            "codex",
            "cursor",
            "openai",
            "anthropic",
            "windsurf",
            ".config",
            ".aws",
            ".azure",
        )
    )


def detect_secret_references(path: Path, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return []
    if stat.st_size > max_bytes:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        if len(line) > 20000:
            continue
        for kind, pattern in SECRET_PROVIDER_PATTERNS:
            for match in pattern.finditer(line):
                secret = match.group(1) if match.lastindex else match.group(0)
                key = (kind, line_no, secret_fingerprint(secret))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    {
                        "kind": kind,
                        "line": line_no,
                        "masked": mask_secret(secret),
                        "fingerprint": secret_fingerprint(secret),
                        "value_recorded": False,
                    }
                )
    return findings


def scan_sensitive_locations(roots: list[Path], max_files: int = 20000) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    visited = 0
    ignored_dirs = {
        ".git",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "AppData",
        "Windows",
        "Program Files",
        "Program Files (x86)",
    }
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in ignored_dirs]
            visited += len(filenames)
            if visited > max_files:
                break
            for filename in filenames:
                path = Path(dirpath) / filename
                if not is_sensitive_name(path) and not should_scan_file_contents(path):
                    continue
                secret_findings = detect_secret_references(path) if should_scan_file_contents(path) else []
                found.append(
                    {
                        "id": stable_id(["secret-location", safe_rel(path)]),
                        "type": "sensitive_file_location",
                        "path": safe_rel(path),
                        "name": path.name,
                        "modified_at": mtime_iso(path),
                        "contents_recorded": False,
                        "secret_findings": secret_findings,
                        "secret_findings_count": len(secret_findings),
                    }
                )
        if visited > max_files:
            break
    return found


def known_chat_locations() -> list[tuple[str, Path]]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming = Path(os.environ.get("APPDATA", ""))
    home = Path.home()
    return [
        ("WeChat", home / "Documents" / "WeChat Files"),
        ("WeChat", roaming / "Tencent" / "WeChat"),
        ("Enterprise WeChat", roaming / "Tencent" / "WeCom"),
        ("DingTalk", roaming / "DingTalk"),
        ("Feishu", roaming / "LarkShell"),
        ("Slack", roaming / "Slack"),
        ("Teams", local / "Packages" / "MSTeams_8wekyb3d8bbwe"),
        ("Teams Classic", roaming / "Microsoft" / "Teams"),
        ("Telegram", roaming / "Telegram Desktop"),
        ("Discord", roaming / "discord"),
    ]


def scan_chat_locations() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for app, path in known_chat_locations():
        if not path.exists():
            continue
        rows.append(
            {
                "id": stable_id(["chat-location", app, safe_rel(path)]),
                "type": "chat_data_location",
                "app": app,
                "path": safe_rel(path),
                "modified_at": mtime_iso(path),
                "contents_recorded": False,
            }
        )
    return rows


def collect_snapshot(state_dir: Path, roots: list[Path]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "host": {
            "platform": platform.platform(),
            "hostname_hash": stable_id(["host", platform.node()]),
            "user_hash": stable_id(["user", os.environ.get("USERNAME") or os.environ.get("USER")]),
        },
        "privacy": {
            "passwords_recorded": False,
            "secret_values_recorded": False,
            "chat_contents_recorded": False,
        },
        "items": (
            scan_environment()
            + scan_installed_apps_from_registry()
            + scan_browser_logins(state_dir)
            + scan_sensitive_locations(roots)
            + scan_chat_locations()
        ),
    }


def item_times(item: dict[str, Any]) -> list[dt.datetime]:
    times: list[dt.datetime] = []
    for key in (
        "created_at",
        "install_date",
        "password_modified_at",
        "modified_at",
        "last_used_at",
        "generated_at",
        "detected_at",
    ):
        value = item.get(key)
        if not value:
            continue
        try:
            times.append(parse_since(value))
        except (ValueError, TypeError):
            continue
    return times


def diff_items(
    current: list[dict[str, Any]], baseline: list[dict[str, Any]], since: dt.datetime
) -> list[dict[str, Any]]:
    baseline_ids = {item.get("id") for item in baseline}
    results: list[dict[str, Any]] = []
    for item in current:
        exists_in_baseline = item.get("id") in baseline_ids
        times = item_times(item)
        after_since = any(when >= since for when in times)
        if not exists_in_baseline or after_since:
            candidate = dict(item)
            candidate["exists_in_baseline"] = exists_in_baseline
            candidate["after_since"] = after_since
            if exists_in_baseline and after_since:
                candidate["cleanup_confidence"] = "needs_review_modified_after_since"
            elif not exists_in_baseline and after_since:
                candidate["cleanup_confidence"] = "high_new_after_since"
            elif not exists_in_baseline:
                candidate["cleanup_confidence"] = "medium_new_but_time_unknown"
            else:
                candidate["cleanup_confidence"] = "low"
            results.append(candidate)
    return results


def group_by_type(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("type", "unknown")), []).append(item)
    return groups


def csv_escape(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("\r", " ")


def write_csv(path: Path, items: list[dict[str, Any]]) -> None:
    fields = [
        "type",
        "cleanup_confidence",
        "browser",
        "profile",
        "origin",
        "username_masked",
        "name",
        "app",
        "path",
        "install_location",
        "created_at",
        "modified_at",
        "install_date",
        "password_modified_at",
        "detected_at",
        "install_paths",
        "secret_findings_count",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow({field: csv_escape(item.get(field)) for field in fields})


def render_report(since: dt.datetime, snapshot: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# Offboarding Cleanup Report",
        "",
        f"- Generated at: `{snapshot.get('generated_at')}`",
        f"- Since: `{since.isoformat()}`",
        "- Password values recorded: `false`",
        "- Secret values recorded: `false`",
        "- Chat contents recorded: `false`",
        "",
        "## Cleanup Policy",
        "",
        "Only remove items that are clearly work-related or were created after the baseline date.",
        "Items marked `needs_review_modified_after_since` existed in the baseline and should not be deleted automatically.",
        "",
    ]
    groups = group_by_type(candidates)
    if not candidates:
        lines.extend(["## Candidates", "", "No candidate items found.", ""])
        return "\n".join(lines)

    lines.extend(["## Candidates", ""])
    for item_type in sorted(groups):
        lines.extend([f"### {item_type}", ""])
        for item in sorted(groups[item_type], key=lambda entry: json.dumps(entry, ensure_ascii=False)):
            confidence = item.get("cleanup_confidence")
            if item_type == "browser_login_metadata":
                label = f"{item.get('browser')} / {item.get('profile')} / {item.get('origin')}"
                details = [
                    f"username={item.get('username_masked') or 'not_recorded'}",
                    f"created={item.get('created_at') or 'unknown'}",
                    f"modified={item.get('password_modified_at') or 'unknown'}",
                ]
            elif item_type == "installed_app":
                label = f"{item.get('name')} {item.get('version') or ''}".strip()
                details = [
                    f"publisher={item.get('publisher') or 'unknown'}",
                    f"install_date={item.get('install_date') or 'unknown'}",
                    f"location={item.get('install_location') or 'unknown'}",
                ]
            elif item_type == "environment_variable":
                label = f"{item.get('scope')}:{item.get('name')}"
                details = ["value_recorded=false"]
            elif item_type == "sensitive_file_location":
                label = item.get("path", "")
                findings = item.get("secret_findings") or []
                kinds = sorted({finding.get("kind") for finding in findings if finding.get("kind")})
                details = [
                    f"modified={item.get('modified_at') or 'unknown'}",
                    "contents_recorded=false",
                    f"secret_findings={len(findings)}",
                ]
                if kinds:
                    details.append(f"kinds={'; '.join(kinds)}")
            elif item_type == "chat_data_location":
                label = f"{item.get('app')} - {item.get('path')}"
                details = [f"modified={item.get('modified_at') or 'unknown'}", "contents_recorded=false"]
            elif item_type == "install_activity_event":
                label = f"install activity detected at {item.get('detected_at') or 'unknown'}"
                install_paths = item.get("install_paths") or []
                details = [
                    f"new_apps={len(item.get('signals', {}).get('new_apps', []))}",
                    f"new_paths={len(item.get('signals', {}).get('new_paths', []))}",
                    f"install_paths={'; '.join(install_paths[:8]) if install_paths else 'unknown'}",
                ]
            else:
                label = item.get("path") or item.get("name") or item.get("id")
                details = []
            lines.append(f"- `{confidence}` {label}")
            if details:
                lines.append(f"  - {', '.join(details)}")
        lines.append("")
    return "\n".join(lines)


def command_init(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir)
    roots = [Path(path) for path in args.scan_root] if args.scan_root else default_scan_roots()
    snapshot = collect_snapshot(state_dir, roots)
    baseline_path = state_dir / BASELINE_FILE
    if baseline_path.exists() and not args.force:
        print(f"Baseline already exists: {baseline_path}")
        print("Use --force to replace it.")
        return 2
    snapshot["baseline_since"] = parse_since(args.since).isoformat()
    write_json(baseline_path, snapshot)
    print(f"Baseline written: {baseline_path}")
    print(f"Items recorded: {len(snapshot['items'])}")
    print("No passwords, secret values, cookies, or chat contents were recorded.")
    return 0


def command_scan(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir)
    roots = [Path(path) for path in args.scan_root] if args.scan_root else default_scan_roots()
    snapshot = collect_snapshot(state_dir, roots)
    snapshot_path = state_dir / SNAPSHOT_FILE
    write_json(snapshot_path, snapshot)
    print(f"Snapshot written: {snapshot_path}")
    print(f"Items recorded: {len(snapshot['items'])}")
    print("No passwords, secret values, cookies, or chat contents were recorded.")
    return 0


def command_report(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir)
    baseline_path = state_dir / BASELINE_FILE
    if not baseline_path.exists():
        print(f"Missing baseline: {baseline_path}", file=sys.stderr)
        print("Run init first.", file=sys.stderr)
        return 2
    baseline = read_json(baseline_path)
    if args.rescan:
        roots = [Path(path) for path in args.scan_root] if args.scan_root else default_scan_roots()
        snapshot = collect_snapshot(state_dir, roots)
        write_json(state_dir / SNAPSHOT_FILE, snapshot)
    else:
        snapshot_path = state_dir / SNAPSHOT_FILE
        if snapshot_path.exists():
            snapshot = read_json(snapshot_path)
        else:
            roots = [Path(path) for path in args.scan_root] if args.scan_root else default_scan_roots()
            snapshot = collect_snapshot(state_dir, roots)
            write_json(snapshot_path, snapshot)

    since = parse_since(args.since or baseline.get("baseline_since") or baseline.get("generated_at"))
    candidates = diff_items(snapshot.get("items", []), baseline.get("items", []), since)
    for event in install_events_since(state_dir / INSTALL_EVENTS_FILE, since):
        item = dict(event)
        item["cleanup_confidence"] = "monitor_install_activity"
        item["exists_in_baseline"] = False
        item["after_since"] = True
        candidates.append(item)
    report = render_report(since, snapshot, candidates)
    report_path = Path(args.output) if args.output else state_dir / REPORT_FILE
    report_path.write_text(report, encoding="utf-8")
    if args.csv:
        write_csv(Path(args.csv), candidates)
    print(f"Report written: {report_path}")
    print(f"Candidate items: {len(candidates)}")
    if args.csv:
        print(f"CSV written: {args.csv}")
    return 0


def load_candidates_for_state(state_dir: Path, since_override: str | None, rescan: bool, scan_roots: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]], dt.datetime]:
    baseline_path = state_dir / BASELINE_FILE
    if not baseline_path.exists():
        raise FileNotFoundError(f"Missing baseline: {baseline_path}")
    baseline = read_json(baseline_path)
    snapshot_path = state_dir / SNAPSHOT_FILE
    if rescan or not snapshot_path.exists():
        roots = [Path(path) for path in scan_roots] if scan_roots else default_scan_roots()
        snapshot = collect_snapshot(state_dir, roots)
        write_json(snapshot_path, snapshot)
    else:
        snapshot = read_json(snapshot_path)
    since = parse_since(since_override or baseline.get("baseline_since") or baseline.get("generated_at"))
    candidates = diff_items(snapshot.get("items", []), baseline.get("items", []), since)
    for event in install_events_since(state_dir / INSTALL_EVENTS_FILE, since):
        item = dict(event)
        item["cleanup_confidence"] = "monitor_install_activity"
        item["exists_in_baseline"] = False
        item["after_since"] = True
        candidates.append(item)
    return snapshot, candidates, since


def command_actions(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir)
    try:
        _snapshot, candidates, _since = load_candidates_for_state(state_dir, args.since, args.rescan, args.scan_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        print("Run init first.", file=sys.stderr)
        return 2
    actions = cleanup_actions_for_items(candidates)
    output_path = Path(args.output) if args.output else state_dir / "cleanup-actions.md"
    if args.format == "json":
        write_json(output_path, {"generated_at": utc_now(), "actions": actions})
    else:
        output_path.write_text(render_cleanup_actions_markdown(actions), encoding="utf-8")
    print(f"Cleanup actions written: {output_path}")
    print(f"Actions: {len(actions)}")
    return 0


def install_events_since(path: Path, since: dt.datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in read_jsonl(path):
        if not signals_have_changes(event.get("signals", {})):
            continue
        detected_at = event.get("detected_at")
        if not detected_at:
            continue
        try:
            if parse_since(str(detected_at)) >= since:
                events.append(event)
        except ValueError:
            continue
    return events


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cleanup_action_for_item(item: dict[str, Any]) -> dict[str, Any]:
    item_type = str(item.get("type", ""))
    action: dict[str, Any] = {
        "id": item.get("id"),
        "source_type": item_type,
        "confidence": item.get("cleanup_confidence"),
        "title": item.get("name") or item.get("path") or item.get("origin") or item.get("id"),
        "risk": "review_required",
        "automatic": False,
        "commands": [],
        "manual_steps": [],
    }

    if item_type == "environment_variable":
        name = str(item.get("name") or "")
        scope = str(item.get("scope") or "user")
        target = "Machine" if scope == "machine" else "User"
        action["title"] = f"{scope}:{name}"
        action["risk"] = "medium"
        action["manual_steps"] = [
            f"Confirm `{name}` was created for work and is no longer needed.",
            "Back up the current value if you may need to restore it.",
            "Remove it from Windows environment variables, then open a new terminal to verify.",
        ]
        if name:
            command = f"[Environment]::SetEnvironmentVariable({powershell_quote(name)}, $null, {powershell_quote(target)})"
            if target == "Machine":
                command += "  # Requires an elevated PowerShell window."
            action["commands"] = [command]
        return action

    if item_type == "browser_login_metadata":
        browser = item.get("browser") or "browser"
        origin = item.get("origin") or "unknown site"
        action["title"] = f"{browser}: {origin}"
        action["risk"] = "high_if_wrong_account"
        action["manual_steps"] = [
            "Open the browser password manager.",
            f"Search for `{origin}`.",
            "Delete only the work-related saved login. Do not delete personal accounts that existed before the baseline.",
            "Clear site cookies/session data for the same work domain if you want to force logout.",
        ]
        if browser == "Chrome":
            action["commands"] = ["start chrome://password-manager/passwords"]
        elif browser == "Edge":
            action["commands"] = ["start msedge://password-manager/passwords"]
        elif browser == "Firefox":
            action["commands"] = ["start firefox about:logins"]
        return action

    if item_type == "sensitive_file_location":
        path = item.get("path") or ""
        findings = item.get("secret_findings") or []
        kinds = sorted({finding.get("kind") for finding in findings if finding.get("kind")})
        action["title"] = str(path)
        action["risk"] = "high_if_file_contains_personal_data"
        action["manual_steps"] = [
            "Open the file and identify whether it contains work API keys, tokens, or credentials.",
            "Revoke tokens from the provider before deleting local copies.",
            "Delete or redact only after confirming it is not needed by personal projects.",
        ]
        if kinds:
            action["detected_secret_kinds"] = kinds
            action["manual_steps"].insert(1, f"Detected secret types: {', '.join(kinds)}.")
            action["manual_steps"].insert(2, "Prefer revoking/rotating these keys at the provider before local cleanup.")
        return action

    if item_type == "chat_data_location":
        app = item.get("app") or "chat app"
        action["title"] = f"{app}: {item.get('path')}"
        action["risk"] = "high_if_chat_history_needed"
        action["manual_steps"] = [
            f"Open {app} and use its official logout or cache cleanup option first.",
            "Confirm whether chat history belongs to a company account or personal account.",
            "Do not delete the data directory directly unless the app is closed and you have confirmed the account scope.",
        ]
        return action

    if item_type == "installed_app":
        action["title"] = str(item.get("name") or "installed app")
        action["risk"] = "medium"
        action["manual_steps"] = [
            "Uninstall from Windows Settings > Apps > Installed apps.",
            "After uninstalling, check the install location and user config directories for leftover work credentials.",
            "Remove related environment variables only after verifying no personal tooling depends on them.",
        ]
        return action

    if item_type == "install_activity_event":
        action["title"] = f"Install activity at {item.get('detected_at')}"
        action["risk"] = "review_required"
        paths = item.get("install_paths") or []
        action["manual_steps"] = [
            "Review the detected app/path changes and decide whether they are work-related.",
            "Use the normal uninstaller for installed applications.",
            "Check detected install paths for config files that may contain work credentials.",
        ]
        action["related_paths"] = paths
        return action

    action["manual_steps"] = ["Review this item manually before taking action."]
    return action


def cleanup_actions_for_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [cleanup_action_for_item(item) for item in items]


def render_cleanup_actions_markdown(actions: list[dict[str, Any]]) -> str:
    lines = [
        "# Cleanup Actions",
        "",
        "This plan contains locations, metadata, and suggested actions only.",
        "It does not contain passwords, token values, cookies, or chat contents.",
        "",
    ]
    if not actions:
        lines.append("No actions.")
        return "\n".join(lines) + "\n"
    for action in actions:
        lines.append(f"## {action.get('title')}")
        lines.append("")
        lines.append(f"- Source type: `{action.get('source_type')}`")
        lines.append(f"- Confidence: `{action.get('confidence')}`")
        lines.append(f"- Risk: `{action.get('risk')}`")
        lines.append(f"- Automatic: `{str(action.get('automatic')).lower()}`")
        commands = action.get("commands") or []
        if commands:
            lines.append("- Commands:")
            for command in commands:
                lines.append(f"  - `{command}`")
        steps = action.get("manual_steps") or []
        if steps:
            lines.append("- Manual steps:")
            for step in steps:
                lines.append(f"  - {step}")
        related_paths = action.get("related_paths") or []
        if related_paths:
            lines.append("- Related paths:")
            for path in related_paths[:20]:
                lines.append(f"  - `{path}`")
        detected_kinds = action.get("detected_secret_kinds") or []
        if detected_kinds:
            lines.append("- Detected secret kinds:")
            for kind in detected_kinds:
                lines.append(f"  - `{kind}`")
        lines.append("")
    return "\n".join(lines)


def command_watch_install(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir)
    state_path = state_dir / INSTALL_MONITOR_STATE_FILE
    events_path = state_dir / INSTALL_EVENTS_FILE
    watch_dirs = [Path(path) for path in args.watch_dir] if args.watch_dir else default_install_watch_dirs()
    interval = max(5, int(args.interval))
    iterations = 1 if args.once else args.iterations

    previous = read_json(state_path) if state_path.exists() else None
    current = install_monitor_snapshot(watch_dirs)
    if previous is None:
        write_json(state_path, current)
        print(f"Install monitor initialized: {state_path}")
        print(f"Watch dirs: {len(current.get('watch_dirs', []))}")
        return 0

    completed = 0
    while True:
        signals = diff_install_monitor_snapshots(previous, current)
        if signals_have_changes(signals):
            event = build_install_event(signals)
            append_jsonl(events_path, event)
            print(f"Install activity recorded: {event['detected_at']}")
            print(f"New apps: {len(signals['new_apps'])}, new paths: {len(signals['new_paths'])}")
        write_json(state_path, current)
        previous = current
        completed += 1
        if iterations is not None and completed >= iterations:
            break
        time.sleep(interval)
        current = install_monitor_snapshot(watch_dirs)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track post-onboarding install/login/config residue without storing secrets."
    )
    parser.add_argument(
        "--state-dir",
        help="Directory that contains the .offboard-assistant state folder. Default: %%APPDATA%%\\OffboardAssistant.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="Create the baseline snapshot.")
    init_parser.add_argument("--since", required=True, help="Baseline date/time, e.g. 2026-07-06.")
    init_parser.add_argument("--scan-root", action="append", default=[], help="Directory to scan for sensitive file locations.")
    init_parser.add_argument("--force", action="store_true", help="Replace an existing baseline.")
    init_parser.set_defaults(func=command_init)

    scan_parser = subcommands.add_parser("scan", help="Create a fresh snapshot.")
    scan_parser.add_argument("--scan-root", action="append", default=[], help="Directory to scan for sensitive file locations.")
    scan_parser.set_defaults(func=command_scan)

    report_parser = subcommands.add_parser("report", help="Generate an offboarding cleanup report.")
    report_parser.add_argument("--since", help="Override the baseline date/time.")
    report_parser.add_argument("--scan-root", action="append", default=[], help="Directory to scan for sensitive file locations.")
    report_parser.add_argument("--output", help="Markdown report path.")
    report_parser.add_argument("--csv", help="Optional CSV export path.")
    report_parser.add_argument("--rescan", action="store_true", help="Rescan before generating the report.")
    report_parser.set_defaults(func=command_report)

    actions_parser = subcommands.add_parser("actions", help="Generate a suggested cleanup action plan.")
    actions_parser.add_argument("--since", help="Override the baseline date/time.")
    actions_parser.add_argument("--scan-root", action="append", default=[], help="Directory to scan for sensitive file locations.")
    actions_parser.add_argument("--output", help="Action plan path.")
    actions_parser.add_argument("--format", choices=["md", "json"], default="md", help="Output format.")
    actions_parser.add_argument("--rescan", action="store_true", help="Rescan before generating actions.")
    actions_parser.set_defaults(func=command_actions)

    watch_parser = subcommands.add_parser(
        "watch-install",
        help="Monitor install-related changes and record lightweight install activity events.",
    )
    watch_parser.add_argument(
        "--watch-dir",
        action="append",
        default=[],
        help="Top-level directory to watch for install-created folders. Defaults to common Windows install dirs.",
    )
    watch_parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds. Minimum: 5.")
    watch_parser.add_argument("--once", action="store_true", help="Run one comparison and exit.")
    watch_parser.add_argument(
        "--iterations",
        type=int,
        help="Number of polling iterations before exit. Omit to keep running.",
    )
    watch_parser.set_defaults(func=command_watch_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
