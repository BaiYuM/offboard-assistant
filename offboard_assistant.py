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
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import winreg
except ImportError:  # pragma: no cover - only available on Windows
    winreg = None  # type: ignore[assignment]


APP_DIR = ".offboard-assistant"
APP_NAME = "OffboardAssistant"
APP_VERSION = "1.0.1"
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

PATH_CATEGORY_RULES = [
    (
        "codex_temp_plugin_cache",
        ("/.codex/.tmp/plugins/",),
        "Codex 临时插件缓存",
        "recommend_cleanup",
    ),
    (
        "codex_temp_cache",
        ("/.codex/.tmp/",),
        "Codex 临时缓存",
        "recommend_cleanup",
    ),
    (
        "ai_tool_config",
        ("/.codex/", "/.claude/", "/.cursor/"),
        "AI/开发工具配置",
        "review_required",
    ),
    (
        "cloud_cli_config",
        ("/.aws/", "/.azure/"),
        "云服务 CLI 配置",
        "review_required",
    ),
    (
        "ssh_config",
        ("/.ssh/",),
        "SSH 配置/密钥",
        "review_required",
    ),
    (
        "environment_file",
        ("/.env",),
        "环境变量文件",
        "review_required",
    ),
    (
        "package_registry_config",
        ("/.npmrc", "/.pypirc"),
        "包管理器认证配置",
        "review_required",
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


# Seed list of SaaS domains whose saved logins / configs in a work account
# almost always belong to the company (rather than the user personally).
# Local config can extend this via `personal_domains` / `company_email_domains`
# in <state_dir>/config.json. Kept small on purpose — false positives are
# worse than misses because the GUI exposes these as `[公司]/[个人]` labels.
KNOWN_SAAS_DOMAINS: frozenset[str] = frozenset({
    "github.com",
    "gitlab.com",
    "notion.so",
    "slack.com",
    "linear.app",
    "figma.com",
    "miro.com",
    "asana.com",
    "jira.atlassian.com",
    "confluence.atlassian.com",
})


# Rules layer (Phase 2): replaces the hardcoded Python constants at
# runtime. Built-ins stay as the bootstrap default; users can add to them
# via ``<state_dir>/rules/overrides.yaml`` without editing source.
RULES_DIR = Path(__file__).resolve().parent / "rules"
DEFAULT_RULES_FILE = RULES_DIR / "default.yaml"
USER_OVERRIDES_FILE = "rules/overrides.yaml"


@dataclass(frozen=True)
class RuleSet:
    """A snapshot of path / SaaS / secret rules merged from builtins + overrides.

    Treat instances as immutable; produce a fresh one via ``load_rules``.
    """

    path_rules: tuple[tuple[str, tuple[str, ...], str, str], ...]
    saas_domains: frozenset[str]
    secret_patterns: tuple[tuple[str, "re.Pattern[str]"], ...]
    overrides_path: str | None

    @property
    def has_overrides(self) -> bool:
        return self.overrides_path is not None


def _load_yaml(path: Path) -> dict[str, Any] | None:
    """Load YAML with PyYAML if installed, otherwise with a stdlib subset
    parser. Returns None if the file is missing or malformed; the caller
    decides what to do.
    """
    if not path.exists():
        return None
    # Preferred: PyYAML.
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        yaml = None  # type: ignore[assignment]
    if yaml is not None:
        try:
            with path.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data
    # Fallback: minimal stdlib YAML parser that handles the schema used
    # by ``rules/default.yaml`` and ``rules/overrides.yaml``: a top-level
    # mapping whose values are either scalar lists or lists of mappings.
    return _load_yaml_subset(path)


def _load_yaml_subset(path: Path) -> dict[str, Any] | None:
    """Tiny YAML parser for the Offboard rules schema. Supports:

    - ``#`` comments to end of line
    - top-level ``key: value`` mappings
    - list entries under a key via ``  - item`` (scalars or ``- key: val``)
    - scalar strings (no quotes required); commas, colons, and ``{`` in
      scalars are tolerated by treating the rest of the line as the value

    Anything more exotic (anchors, multi-line scalars, flow style) falls
    through and returns ``None``, signalling the caller to fall back to
    built-in constants. This is intentional: the file is hand-written, the
    schema is fixed, and the tool never crashes on a feature it doesn't
    understand.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    result: dict[str, Any] = {}

    def indent_of(raw: str) -> int:
        return len(raw) - len(raw.lstrip(" "))

    def strip_comment(raw: str) -> str:
        return raw.split("#", 1)[0].rstrip()

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = strip_comment(raw)
        if not line.strip():
            i += 1
            continue
        if indent_of(line) != 0:
            return None  # unexpected indent at top level
        if ":" not in line:
            return None
        key, _ = line.split(":", 1)
        key = key.strip()
        i += 1
        # Collect indented block under this key.
        items: list[Any] = []
        # Top-level list indent is expected at indent == 2 (e.g., "  - foo").
        while i < len(lines):
            raw = lines[i]
            cur = strip_comment(raw)
            if not cur.strip():
                i += 1
                continue
            ind = indent_of(cur)
            if ind == 0:
                break  # next top-level key
            if ind != 2:
                return None
            if not cur.lstrip().startswith("- "):
                return None
            item_text = cur.lstrip()[2:].strip()
            if not item_text:
                i += 1
                continue
            if ":" in item_text and not item_text.startswith(('"', "'")):
                sub_key, _, sub_val = item_text.partition(":")
                sub_map: dict[str, Any] = {sub_key.strip(): _coerce(sub_val.strip())}
                i += 1
                # Consume deeper-indented key: value pairs OR list entries
                # belonging to this sub-mapping. The depth must be greater
                # than the "- " position (>= 4 spaces). A "-" at this depth
                # starts a list under the most recently-seen key.
                current_key = sub_key.strip()
                while i < len(lines):
                    raw = lines[i]
                    inner = strip_comment(raw)
                    if not inner.strip():
                        i += 1
                        continue
                    if indent_of(inner) <= 2:
                        break
                    stripped = inner.lstrip()
                    if stripped.startswith("- "):
                        # List entry under the most recent key.
                        entry = stripped[2:].strip()
                        existing = sub_map.get(current_key)
                        if isinstance(existing, list):
                            existing.append(_coerce(entry))
                        else:
                            sub_map[current_key] = [_coerce(entry)]
                        i += 1
                        continue
                    if ":" not in inner:
                        return None
                    k2, _, v2 = inner.strip().partition(":")
                    k2 = k2.strip()
                    sub_map[k2] = _coerce(v2.strip())
                    current_key = k2
                    i += 1
                items.append(sub_map)
            else:
                items.append(_coerce(item_text))
                i += 1
        result[key] = items
    return result


def _coerce(value: str) -> Any:
    """Best-effort scalar coercion for the stdlib YAML subset.

    Strips a single matching pair of surrounding ``"`` or ``'`` (a YAML
    convention we follow in our hand-written files) and returns strings
    as-is unless they look like an obvious number/boolean.
    """
    if not value:
        return ""
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in {"null", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _materialize_default_rules() -> RuleSet:
    """Read rules/default.yaml if available; otherwise fall back to the
    hardcoded Python constants (so the tool still works without PyYAML).
    """
    yaml_data = _load_yaml(DEFAULT_RULES_FILE)
    if yaml_data is None:
        # Fallback path: PyYAML missing or default.yaml unreadable. Use
        # the Python constants shipped with the module.
        return RuleSet(
            path_rules=tuple((cat, tuple(needles), label, rec) for cat, needles, label, rec in PATH_CATEGORY_RULES),
            saas_domains=KNOWN_SAAS_DOMAINS,
            secret_patterns=tuple((kind, pattern) for kind, pattern in SECRET_PROVIDER_PATTERNS),
            overrides_path=None,
        )

    path_rules = yaml_data.get("path_rules") or []
    materialized_path: list[tuple[str, tuple[str, ...], str, str]] = []
    for entry in path_rules:
        if not isinstance(entry, dict):
            continue
        cat = str(entry.get("category", "")).strip()
        needles = entry.get("needles") or []
        label = str(entry.get("label", cat)).strip()
        rec = str(entry.get("recommendation", "review_required")).strip()
        if not cat or not needles:
            continue
        materialized_path.append((cat, tuple(str(n) for n in needles), label, rec))

    saas = yaml_data.get("saas_domains") or []
    materialized_saas: set[str] = {str(d).lower() for d in saas if d}

    patterns = yaml_data.get("secret_patterns") or []
    materialized_patterns: list[tuple[str, re.Pattern[str]]] = []
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "")).strip()
        pattern = str(entry.get("pattern", "")).strip()
        if not kind or not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        materialized_patterns.append((kind, compiled))

    return RuleSet(
        path_rules=tuple(materialized_path),
        saas_domains=frozenset(materialized_saas),
        secret_patterns=tuple(materialized_patterns),
        overrides_path=None,
    )


def load_rules(state_dir: Path | None = None) -> RuleSet:
    """Load the merged rule set for a given state directory.

    Merges the built-in defaults (from ``rules/default.yaml`` or the
    Python constant fallback) with any user-supplied
    ``<state_dir>/rules/overrides.yaml``. Override semantics:

    - ``saas_domains``: **additive** — both sets unioned.
    - ``path_rules``: per-category additive — same ``category`` key
      extends needles; new categories appended after built-ins.
    - ``secret_patterns``: **additive** — append override patterns
      after built-ins.
    """
    base = _materialize_default_rules()
    if state_dir is None:
        return base
    overrides_path = state_dir / USER_OVERRIDES_FILE
    overrides = _load_yaml(overrides_path)
    if overrides is None:
        return RuleSet(
            path_rules=base.path_rules,
            saas_domains=base.saas_domains,
            secret_patterns=base.secret_patterns,
            overrides_path=None,
        )

    # Merge SaaS domains (additive).
    merged_saas: set[str] = set(base.saas_domains)
    for d in overrides.get("saas_domains") or []:
        if isinstance(d, str) and d:
            merged_saas.add(d.lower())

    # Merge path rules (per-category additive).
    rules_by_cat: dict[str, list[str]] = {}
    ordered_cats: list[str] = []
    rule_meta: dict[str, tuple[str, str]] = {}
    for cat, needles, label, rec in base.path_rules:
        rules_by_cat.setdefault(cat, []).extend(needles)
        if cat not in ordered_cats:
            ordered_cats.append(cat)
        rule_meta[cat] = (label, rec)
    for entry in overrides.get("path_rules") or []:
        if not isinstance(entry, dict):
            continue
        cat = str(entry.get("category", "")).strip()
        needles = entry.get("needles") or []
        if not cat or not needles:
            continue
        rules_by_cat.setdefault(cat, []).extend(str(n) for n in needles)
        if cat not in ordered_cats:
            ordered_cats.append(cat)
        # Override metadata wins for that category.
        label = str(entry.get("label") or rule_meta.get(cat, (cat,))[0])
        rec = str(entry.get("recommendation") or rule_meta.get(cat, (cat, "review_required"))[1])
        rule_meta[cat] = (label, rec)
    merged_path: list[tuple[str, tuple[str, ...], str, str]] = []
    for cat in ordered_cats:
        label, rec = rule_meta[cat]
        merged_path.append((cat, tuple(rules_by_cat[cat]), label, rec))

    # Merge secret patterns (additive, in order: builtins then overrides).
    merged_patterns: list[tuple[str, re.Pattern[str]]] = list(base.secret_patterns)
    for entry in overrides.get("secret_patterns") or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "")).strip()
        pattern = str(entry.get("pattern", "")).strip()
        if not kind or not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        merged_patterns.append((kind, compiled))

    return RuleSet(
        path_rules=tuple(merged_path),
        saas_domains=frozenset(merged_saas),
        secret_patterns=tuple(merged_patterns),
        overrides_path=str(overrides_path),
    )


def infer_account_owner_hint(item: dict[str, Any], config: dict[str, Any] | None = None, state_dir: Path | None = None) -> str:
    """Return `company_account`, `personal_account`, or `unknown` for an item.

    Pure local substring matching against `origin` / `path` / `title` fields.
    Never reads file contents, never makes a network call.

    The SaaS domain seed list is loaded from ``rules/default.yaml`` (with
    Python-constant fallback when PyYAML is missing) and merged with any
    user overrides at ``<state_dir>/rules/overrides.yaml``.
    """
    cfg = config or {}
    company_domains = [str(d).lower() for d in cfg.get("company_email_domains", []) if d]
    personal_domains = [str(d).lower() for d in cfg.get("personal_email_domains", []) if d]

    # Resolve the SaaS seed list from the rules layer so user overrides
    # in <state_dir>/rules/overrides.yaml actually take effect.
    rules = load_rules(state_dir)
    saas_domains = set(rules.saas_domains)

    haystacks: list[str] = []
    for key in ("origin", "path", "title", "name", "app", "username_masked"):
        value = item.get(key)
        if isinstance(value, str) and value:
            haystacks.append(value.lower())

    # Explicit user config wins first.
    for domain in company_domains:
        if any(domain in h for h in haystacks):
            return "company_account"
    for domain in personal_domains:
        if any(domain in h for h in haystacks):
            return "personal_account"

    # Personal tool paths in the user's home directory are usually personal.
    for h in haystacks:
        if any(
            marker in h
            for marker in (
                "/.codex/", "/.claude/", "/.cursor/", "/.gemini/", "/.openai/",
                "\\.codex\\", "\\.claude\\", "\\.cursor\\", "\\.gemini\\", "\\.openai\\",
            )
        ):
            return "personal_account"

    # SaaS origin / config path with no personal marker — assume company.
    for h in haystacks:
        for domain in saas_domains:
            if domain in h:
                return "company_account"

    return "unknown"


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


def normalize_path_for_match(path: str) -> str:
    return path.replace("/", "\\").lower()


def categorize_path(path: str, config: dict[str, Any] | None = None, state_dir: Path | None = None) -> dict[str, str]:
    slash_normalized = path.replace("\\", "/").lower()
    rules = load_rules(state_dir)
    for category, needles, label, recommendation in rules.path_rules:
        for needle in needles:
            if needle.lower() in slash_normalized:
                return {
                    "category": category,
                    "category_label": label,
                    "recommendation": recommendation,
                    "account_owner_hint": infer_account_owner_hint({"path": path}, config),
                }
    return {
        "category": "sensitive_file",
        "category_label": "敏感文件位置",
        "recommendation": "review_required",
        "account_owner_hint": infer_account_owner_hint({"path": path}, config),
    }


def recommended_cleanup_target(item: dict[str, Any]) -> str | None:
    if item.get("type") != "sensitive_file_location":
        return None
    if item.get("recommendation") != "recommend_cleanup":
        return None
    path = str(item.get("path") or "")
    if not path:
        return None
    parts = Path(path).parts
    lowered = [part.lower() for part in parts]
    category = item.get("category")
    if category == "codex_temp_plugin_cache":
        try:
            dot_codex = lowered.index(".codex")
            if lowered[dot_codex + 1] == ".tmp" and lowered[dot_codex + 2] == "plugins":
                return str(Path(*parts[: dot_codex + 3]))
        except (ValueError, IndexError):
            return str(Path(path).parent)
    if category == "codex_temp_cache":
        try:
            dot_codex = lowered.index(".codex")
            if lowered[dot_codex + 1] == ".tmp":
                return str(Path(*parts[: dot_codex + 2]))
        except (ValueError, IndexError):
            return str(Path(path).parent)
    return path


def ai_review_payload_for_items(
    items: list[dict[str, Any]],
    state_dir: Path | None = None,
) -> dict[str, Any]:
    review_items: list[dict[str, Any]] = []
    for item in items:
        findings = item.get("secret_findings") or []
        review_items.append(
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "cleanup_confidence": item.get("cleanup_confidence"),
                "category": item.get("category"),
                "category_label": item.get("category_label"),
                "recommendation": item.get("recommendation"),
                "account_owner_hint": item.get("account_owner_hint") or "unknown",
                "path": item.get("path"),
                "cleanup_target_path": recommended_cleanup_target(item),
                "origin": item.get("origin"),
                "browser": item.get("browser"),
                "profile": item.get("profile"),
                "app": item.get("app"),
                "name": item.get("name"),
                "secret_findings_count": len(findings),
                "secret_kinds": sorted({finding.get("kind") for finding in findings if finding.get("kind")}),
                "value_recorded": False,
                "contents_recorded": item.get("contents_recorded", False),
                "password_recorded": item.get("password_recorded", False),
            }
        )
    payload: dict[str, Any] = {
        "generated_at": utc_now(),
        "purpose": "AI review payload for cleanup recommendations. Contains metadata only, not secret values.",
        "safety_rules": [
            "Do not recommend deleting browser passwords or chat directories without explicit user confirmation.",
            "Recommend revoking or rotating API keys before local cleanup.",
            "Prefer quarantine/move-to-backup over permanent deletion.",
            "Treat user-handled items as negative examples: do NOT re-select them.",
        ],
        "items": review_items,
    }
    if state_dir is not None:
        handled_path = state_dir / "handled-items.json"
        if handled_path.exists():
            try:
                handled_raw = read_json(handled_path).get("items", [])
            except (OSError, ValueError):
                handled_raw = []
            payload["user_feedback"] = {
                # Whitelist: only {id, type, handled_at}. Drop title/path/origin
                # so handled history never re-leaks identifiers into the AI.
                "handled_items": [
                    {
                        "id": h.get("id"),
                        "type": h.get("type"),
                        "handled_at": h.get("handled_at"),
                    }
                    for h in handled_raw
                    if h.get("id")
                ]
            }
    return payload


def ensure_state_dir(base: Path) -> Path:
    # Persist absolute paths in quarantine manifests even when callers pass a
    # relative --state-dir.  Avoid resolve() here so Windows keeps the user's
    # long-path spelling instead of rewriting it to an 8.3 alias.
    base = base.expanduser().absolute()
    state_dir = base / APP_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def default_state_base() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / APP_NAME


def portable_state_base() -> Path:
    """Return the portable state base, used when ``--portable`` is set.

    Layout: ``<exe parent>/.offboard_data``. When running from source
    (not frozen) the parent falls back to cwd so the dev workflow still
    works without an exe.
    """
    exe = getattr(sys, "executable", None)
    if exe:
        # ``resolve()`` can rewrite Windows temp paths to their 8.3 alias,
        # which makes the portable directory appear to move.  ``absolute()``
        # keeps the executable's spelling while still handling relative paths.
        parent = Path(exe).absolute().parent
    else:
        parent = Path.cwd()
    return parent / ".offboard_data"


def state_dir_from_arg(value: str | None, portable: bool = False) -> Path:
    if value:
        return ensure_state_dir(Path(value))
    base = portable_state_base() if portable else default_state_base()
    base.mkdir(parents=True, exist_ok=True)
    state_dir = ensure_state_dir(base)
    migrate_legacy_state_if_needed(state_dir)
    return state_dir


# Local-only config (NOT added to SYNC_FILES on purpose: company domain
# rules are user-specific, never synced to cloud).
CONFIG_FILE = "config.json"
WIZARD_DONE_FILE = "wizard.done"
DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    # The wizard stores the user's intended baseline date here so the GUI can
    # restore the field on the next launch.  Keep the empty value for
    # backwards-compatible loading of existing config files.
    "baseline_since": "",
    "scan_roots": [],
    "excluded_paths": [],
    "company_email_domains": [],
    "personal_email_domains": [],
    "ide_scan_enabled": True,
}


def default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def load_local_config(state_dir: Path) -> dict[str, Any]:
    path = state_dir / CONFIG_FILE
    if not path.exists():
        return default_config()
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return default_config()
    merged = default_config()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in merged and isinstance(value, type(merged[key])):
                merged[key] = value
    return merged


def save_local_config(state_dir: Path, config: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json(state_dir / CONFIG_FILE, config)


def is_wizard_done(state_dir: Path) -> bool:
    return (state_dir / WIZARD_DONE_FILE).exists()


def mark_wizard_done(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / WIZARD_DONE_FILE).write_text(
        json.dumps({"completed_at": utc_now()}, ensure_ascii=False),
        encoding="utf-8",
    )


def resolve_scan_roots(args_scan_roots: list[str] | None, config: dict[str, Any]) -> list[Path]:
    """Merge scan-roots with precedence: CLI > config > builtin default."""
    seen: set[str] = set()
    result: list[Path] = []
    for raw in args_scan_roots or []:
        p = Path(raw).resolve()
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    for raw in config.get("scan_roots") or []:
        try:
            p = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    if not result:
        return default_scan_roots()
    return result


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
    for name in sorted(DEFAULT_SCAN_DIR_NAMES):
        candidate = home / name
        if candidate.exists():
            roots.append(candidate)
    for name in sorted(AI_APP_CONFIG_DIR_NAMES):
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


def detect_secret_references(path: Path, max_bytes: int = 2 * 1024 * 1024, state_dir: Path | None = None) -> list[dict[str, Any]]:
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
    rules = load_rules(state_dir)
    for line_no, line in enumerate(text.splitlines(), start=1):
        if len(line) > 20000:
            continue
        for kind, pattern in rules.secret_patterns:
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


def scan_sensitive_locations(
    roots: list[Path],
    max_files: int = 20000,
    state_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    visited = 0
    excluded_paths: list[Path] = []
    for value in (config or {}).get("excluded_paths", []):
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            excluded_paths.append(Path(value).expanduser().resolve(strict=False))
        except (OSError, RuntimeError):
            continue

    def is_excluded(path: Path) -> bool:
        try:
            candidate = path.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        return any(candidate == excluded or excluded in candidate.parents for excluded in excluded_paths)

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
    limit_reached = False
    for root in roots:
        if not root.exists() or is_excluded(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            current_dir = Path(dirpath)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in ignored_dirs and not is_excluded(current_dir / name)
            )
            for filename in sorted(filenames):
                if visited >= max_files:
                    limit_reached = True
                    break
                visited += 1
                path = current_dir / filename
                if not is_sensitive_name(path) and not should_scan_file_contents(path):
                    continue
                secret_findings = detect_secret_references(path, state_dir=state_dir) if should_scan_file_contents(path) else []
                path_text = safe_rel(path)
                category = categorize_path(path_text, config=config, state_dir=state_dir)
                if secret_findings:
                    category = dict(category)
                    category["recommendation"] = "prioritize_revoke_then_clean"
                found.append(
                    {
                        "id": stable_id(["secret-location", path_text]),
                        "type": "sensitive_file_location",
                        "path": path_text,
                        "name": path.name,
                        "modified_at": mtime_iso(path),
                        "contents_recorded": False,
                        "secret_findings": secret_findings,
                        "secret_findings_count": len(secret_findings),
                        **category,
                    }
                )
            if limit_reached:
                break
        if limit_reached:
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
                "account_scope": "unknown",
                "recommendation": "chat_account_review",
                "category": "chat_data",
                "category_label": "聊天数据位置",
            }
        )
    return rows


def scan_recent_ide_projects(custom_roots: list[Path] | None = None) -> list[dict[str, Any]]:
    """Discover recently-opened IDE projects without reading any sensitive content.

    Privacy boundary (per SECURITY.md):
      - Parses ONLY JetBrains-family ``recentProjects.xml`` (plaintext XML).
      - Never reads ``state.vscdb`` (VSCode/Cursor SQLite) — it can contain
        cached extension tokens.
      - Never reads ``workspaceStorage/<hash>/workspace.json`` ``settings``
        subtree — it can contain ``sentry.dsn`` and other token references.
      - Never reads ``argv.json`` — it can contain full local install paths.
      - Never reads any file content beyond XML element text.
    """
    roots: list[Path] = []
    if custom_roots:
        roots.extend(custom_roots)
    else:
        # Platform-specific discovery. Each block is best-effort: missing
        # directories simply yield no matches, never an error.
        if sys.platform.startswith("win"):
            appdata = os.environ.get("APPDATA")
            if appdata:
                roots.append(Path(appdata) / "JetBrains")
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                roots.append(Path(local_appdata) / "JetBrains")
        elif sys.platform == "darwin":
            # macOS: ~/Library/Application Support/JetBrains
            roots.append(Path.home() / "Library" / "Application Support" / "JetBrains")
        else:
            # Linux / other Unix: respect XDG_CONFIG_HOME, then ~/.config
            xdg = os.environ.get("XDG_CONFIG_HOME")
            if xdg:
                roots.append(Path(xdg) / "JetBrains")
            roots.append(Path.home() / ".config" / "JetBrains")

    if not roots:
        return []

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for xml_path in glob.glob(str(root / "*" / "options" / "recentProjects.xml")):
            ide_name = Path(xml_path).parent.parent.name
            try:
                tree = ET.parse(xml_path)
            except (ET.ParseError, OSError, ValueError):
                # Schema drift between JetBrains versions; skip silently.
                continue
            try:
                for entry in tree.getroot().findall(".//entry"):
                    path = entry.get("key") or entry.get("path")
                    if not path:
                        continue
                    meta = entry.find("value/RecentProjectMetaInfo")
                    project_name: str | None = None
                    last_opened_raw: str | None = None
                    if meta is not None:
                        for opt in meta.findall("option"):
                            oname = opt.get("name")
                            if oname == "projectName":
                                project_name = opt.get("value")
                            elif oname == "lastOpened":
                                last_opened_raw = opt.get("value")
                    display_name = project_name or Path(path).name or "unknown"
                    last_opened_iso = _jetbrains_time_to_iso(last_opened_raw)
                    item_id = stable_id(["ide-recent", ide_name, path])
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    rows.append(
                        {
                            "id": item_id,
                            "type": "ide_recent_project",
                            "ide": ide_name,
                            "path": path,
                            "name": display_name,
                            "last_opened_at": last_opened_iso,
                            "category": "ide_recent_project",
                            "category_label": "IDE 最近项目",
                            "recommendation": "review_required",
                            "account_owner_hint": infer_account_owner_hint(
                                {"path": path, "name": display_name, "ide": ide_name}
                            ),
                            "contents_recorded": False,
                            "value_recorded": False,
                            "modified_at": last_opened_iso,
                        }
                    )
            finally:
                # Release the parsed tree as early as possible so the on-disk
                # XML payload is not retained in memory longer than needed.
                del tree
    return rows


def _jetbrains_time_to_iso(value: str | None) -> str | None:
    """Convert JetBrains ``lastOpened`` (epoch millis or ISO) to ISO 8601 UTC."""
    if not value:
        return None
    try:
        millis = int(value)
        if millis > 10**12:  # treat as milliseconds
            return dt.datetime.fromtimestamp(millis / 1000.0, tz=dt.timezone.utc).isoformat()
        return dt.datetime.fromtimestamp(millis, tz=dt.timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc).isoformat()
    except ValueError:
        return None


def collect_snapshot(
    state_dir: Path,
    roots: list[Path],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_config = config if config is not None else load_local_config(state_dir)
    items = (
        scan_environment()
        + scan_installed_apps_from_registry()
        + scan_browser_logins(state_dir)
        + scan_sensitive_locations(roots, state_dir=state_dir, config=effective_config)
        + scan_chat_locations()
    )
    if effective_config.get("ide_scan_enabled", True):
        items.extend(scan_recent_ide_projects())
    for item in items:
        item["account_owner_hint"] = infer_account_owner_hint(
            item,
            effective_config,
            state_dir=state_dir,
        )
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
            "ide_recent_project_contents_recorded": False,
        },
        "items": items,
    }


def item_times(item: dict[str, Any]) -> list[dt.datetime]:
    times: list[dt.datetime] = []
    for key in (
        "created_at",
        "install_date",
        "password_modified_at",
        "modified_at",
        "last_used_at",
        "last_opened_at",
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
                    f"category={item.get('category_label') or 'unknown'}",
                    f"recommendation={item.get('recommendation') or 'review_required'}",
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
    state_dir = state_dir_from_arg(args.state_dir, portable=bool(getattr(args, "portable", False)))
    config = _load_config_for_args(args, state_dir)
    roots = resolve_scan_roots(args.scan_root, config)
    snapshot = collect_snapshot(state_dir, roots, config=config)
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
    state_dir = state_dir_from_arg(args.state_dir, portable=bool(getattr(args, "portable", False)))
    config = _load_config_for_args(args, state_dir)
    roots = resolve_scan_roots(args.scan_root, config)
    snapshot = collect_snapshot(state_dir, roots, config=config)
    snapshot_path = state_dir / SNAPSHOT_FILE
    write_json(snapshot_path, snapshot)
    print(f"Snapshot written: {snapshot_path}")
    print(f"Items recorded: {len(snapshot['items'])}")
    print("No passwords, secret values, cookies, or chat contents were recorded.")
    return 0


def command_report(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir, portable=bool(getattr(args, "portable", False)))
    baseline_path = state_dir / BASELINE_FILE
    if not baseline_path.exists():
        print(f"Missing baseline: {baseline_path}", file=sys.stderr)
        print("Run init first.", file=sys.stderr)
        return 2
    baseline = read_json(baseline_path)
    config = _load_config_for_args(args, state_dir)
    roots = resolve_scan_roots(args.scan_root, config)
    if args.rescan:
        snapshot = collect_snapshot(state_dir, roots, config=config)
        write_json(state_dir / SNAPSHOT_FILE, snapshot)
    else:
        snapshot_path = state_dir / SNAPSHOT_FILE
        if snapshot_path.exists():
            snapshot = read_json(snapshot_path)
        else:
            snapshot = collect_snapshot(state_dir, roots, config=config)
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


def load_candidates_for_state(state_dir: Path, since_override: str | None, rescan: bool, scan_roots: list[str], config: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], dt.datetime]:
    baseline_path = state_dir / BASELINE_FILE
    if not baseline_path.exists():
        raise FileNotFoundError(f"Missing baseline: {baseline_path}")
    baseline = read_json(baseline_path)
    snapshot_path = state_dir / SNAPSHOT_FILE
    if rescan or not snapshot_path.exists():
        roots = resolve_scan_roots(scan_roots, config or {})
        snapshot = collect_snapshot(state_dir, roots, config=config)
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


def command_list_rules(args: argparse.Namespace) -> int:
    """Print the merged rule set. With ``--overrides-path``, also show
    the path the loader is reading from (or "no overrides")."""
    state_dir = state_dir_from_arg(args.state_dir, portable=bool(getattr(args, "portable", False)))
    rules = load_rules(state_dir)
    print("Path rules:")
    for cat, needles, label, rec in rules.path_rules:
        needle_list = ", ".join(needles)
        print(f"  - {cat} ({rec}): {needle_list}")
    print("")
    print("SaaS domains:")
    for domain in sorted(rules.saas_domains):
        print(f"  - {domain}")
    print("")
    print("Secret patterns:")
    for kind, _pattern in rules.secret_patterns:
        print(f"  - {kind}")
    if args.overrides_path:
        path = state_dir / USER_OVERRIDES_FILE
        print("")
        print(f"Overrides: {path if path.exists() else '(no overrides)'}")
    return 0


def command_actions(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir, portable=bool(getattr(args, "portable", False)))
    try:
        config = _load_config_for_args(args, state_dir)
        _snapshot, candidates, _since = load_candidates_for_state(state_dir, args.since, args.rescan, args.scan_root, config)
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


def list_quarantine_bundles(state_dir: Path) -> list[dict[str, Any]]:
    quarantine_root = state_dir / "quarantine"
    if not quarantine_root.exists():
        return []
    bundles: list[dict[str, Any]] = []
    for child in sorted(quarantine_root.iterdir(), reverse=True):
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = read_json(manifest_path)
        except (OSError, ValueError):
            bundles.append({"directory": str(child), "ts": child.name, "items": [], "errors": ["manifest_unreadable"]})
            continue
        if not isinstance(data, dict):
            bundles.append({"directory": str(child), "ts": child.name, "items": [], "errors": ["manifest_invalid_shape"]})
            continue
        items = data.get("items", [])
        errors = data.get("errors", [])
        bundles.append(
            {
                "directory": str(child),
                "ts": child.name,
                "items": items if isinstance(items, list) else [],
                "errors": errors if isinstance(errors, list) else [],
            }
        )
    return bundles


QUARANTINE_INDEX_FILE = "quarantine-index.sqlite"


def _quarantine_index_path(state_dir: Path) -> Path:
    return state_dir / QUARANTINE_INDEX_FILE


def _ensure_quarantine_index(state_dir: Path) -> "sqlite3.Connection | None":
    """Open the quarantine SQLite index, creating schema if missing.

    Returns None on filesystem error so callers can degrade to manifest.json
    reads without the index (it is an accelerator, never the source of truth).
    """
    try:
        path = _quarantine_index_path(state_dir)
        conn = sqlite3.connect(str(path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                source_bytes INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '',
                restored_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                batch_id TEXT NOT NULL,
                item_id TEXT,
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                category TEXT,
                moved_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_batch ON items(batch_id)"
        )
        conn.commit()
        return conn
    except sqlite3.Error:
        return None


def _quarantine_path_size(path: Path) -> int:
    """Return the logical bytes stored at an indexed quarantine destination."""
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0

    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        directory = Path(dirpath)
        for name in filenames:
            try:
                total += (directory / name).lstat().st_size
            except OSError:
                continue
        for name in dirnames:
            child = directory / name
            try:
                if child.is_symlink():
                    total += child.lstat().st_size
            except OSError:
                continue
    return total


def _index_quarantine_batch(state_dir: Path, batch_dir: Path, manifest: dict[str, Any]) -> None:
    """Mirror a freshly written manifest into the SQLite index.

    Best-effort: errors are swallowed because the index is an accelerator,
    not the source of truth. The manifest.json still drives restore.
    """
    conn = _ensure_quarantine_index(state_dir)
    if conn is None:
        return
    try:
        batch_id = batch_dir.name
        raw_items = manifest.get("items", []) or []
        items = [row for row in raw_items if isinstance(row, dict)] if isinstance(raw_items, list) else []
        source_bytes = 0
        try:
            resolved_batch = batch_dir.resolve()
        except (OSError, RuntimeError):
            resolved_batch = None
        for row in items:
            destination_value = row.get("destination")
            if not isinstance(destination_value, str) or not destination_value.strip():
                continue
            try:
                destination = Path(destination_value)
                resolved_destination = destination.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved_batch is None or resolved_destination == resolved_batch:
                continue
            if not resolved_destination.is_relative_to(resolved_batch):
                continue
            source_bytes += _quarantine_path_size(destination)
        tags = ""
        conn.execute(
            "INSERT OR REPLACE INTO batches (batch_id, created_at, source_count, source_bytes, tags, restored_count) VALUES (?, ?, ?, ?, ?, 0)",
            (
                batch_id,
                utc_now(),
                len(items),
                source_bytes,
                tags,
            ),
        )
        # Replace any existing items for this batch (idempotent re-index).
        conn.execute("DELETE FROM items WHERE batch_id = ?", (batch_id,))
        for row in items:
            conn.execute(
                "INSERT INTO items (batch_id, item_id, source, destination, category, moved_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    str(row.get("item_id") or ""),
                    str(row.get("source") or ""),
                    str(row.get("destination") or ""),
                    str(row.get("category") or ""),
                    str(row.get("moved_at") or ""),
                ),
            )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def query_quarantine_index(state_dir: Path, limit: int = 100) -> list[dict[str, Any]]:
    """Return recent batches from the SQLite index. Fast (O(1)) unlike the
    manifest.json scan path. Empty list when the index does not exist yet.
    """
    conn = _ensure_quarantine_index(state_dir)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT batch_id, created_at, source_count, source_bytes, tags, restored_count "
            "FROM batches ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "batch_id": r[0],
                "created_at": r[1],
                "source_count": r[2],
                "source_bytes": r[3],
                "tags": r[4],
                "restored_count": r[5],
            }
            for r in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _bump_quarantine_index_restored(state_dir: Path, batch_id: str, restored_count: int) -> None:
    """Update the index after a restore. Silent on failure."""
    conn = _ensure_quarantine_index(state_dir)
    if conn is None:
        return
    try:
        conn.execute(
            "UPDATE batches SET restored_count = ? WHERE batch_id = ?",
            (restored_count, batch_id),
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _remove_quarantine_index_batch(state_dir: Path, batch_id: str) -> None:
    """Remove a purged batch from an existing index without creating one."""
    index_path = _quarantine_index_path(state_dir)
    if not index_path.is_file():
        return
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(index_path))
        conn.execute("DELETE FROM items WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM batches WHERE batch_id = ?", (batch_id,))
        conn.commit()
    except sqlite3.Error:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def restore_quarantine_dir(quarantine_dir: Path) -> dict[str, Any]:
    """Reverse `quarantine_selected_recommended` by reading manifest.json and
    moving each entry back to its original path. Skips (does not overwrite)
    destinations that already exist; reports both groups.
    """
    manifest_path = quarantine_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    result: dict[str, Any] = {"restored": [], "skipped": [], "errors": []}
    if not isinstance(manifest, dict):
        result["errors"].append("manifest must be a JSON object")
        return result
    items = manifest.get("items", [])
    if not isinstance(items, list):
        result["errors"].append("manifest items must be a list")
        return result
    try:
        resolved_batch = quarantine_dir.resolve()
        resolved_manifest = manifest_path.resolve()
    except (OSError, RuntimeError) as exc:
        result["errors"].append(f"could not resolve quarantine paths: {exc}")
        return result

    for index, row in enumerate(items):
        error_prefix = f"manifest item {index}"
        if not isinstance(row, dict):
            result["errors"].append(f"{error_prefix}: item must be an object")
            continue
        source_value = row.get("source")
        destination_value = row.get("destination")
        if not isinstance(source_value, str) or not source_value.strip():
            result["errors"].append(f"{error_prefix}: source must be a non-empty string")
            continue
        if not isinstance(destination_value, str) or not destination_value.strip():
            result["errors"].append(f"{error_prefix}: destination must be a non-empty string")
            continue
        source = Path(source_value)
        destination = Path(destination_value)
        if not source.is_absolute() or not destination.is_absolute():
            result["errors"].append(f"{error_prefix}: source and destination must be absolute paths")
            continue
        try:
            resolved_source = source.resolve()
            resolved_destination = destination.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            result["errors"].append(f"{error_prefix}: invalid path: {exc}")
            continue
        if resolved_destination == resolved_batch or not resolved_destination.is_relative_to(resolved_batch):
            result["errors"].append(f"{error_prefix}: destination must be inside the quarantine batch")
            continue
        if resolved_destination == resolved_manifest:
            result["errors"].append(f"{error_prefix}: destination cannot be the manifest")
            continue
        if resolved_source.is_relative_to(resolved_batch):
            result["errors"].append(f"{error_prefix}: source cannot be inside the quarantine batch")
            continue
        if not destination.exists():
            result["errors"].append(f"quarantined file missing: {destination}")
            continue
        if source.exists():
            result["skipped"].append(str(source))
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
            result["restored"].append(str(source))
        except OSError as exc:
            result["errors"].append(f"{source}: {exc}")
    if result["restored"]:
        # Derive state_dir from the path layout: <state_dir>/quarantine/<ts>.
        # Skip silently if the layout doesn't match (e.g. ad-hoc restore).
        if quarantine_dir.parent.name == "quarantine":
            state_dir = quarantine_dir.parent.parent
            _bump_quarantine_index_restored(state_dir, quarantine_dir.name, len(result["restored"]))
    return result


def purge_quarantine_dir(quarantine_dir: Path) -> dict[str, Any]:
    """Permanently delete a quarantine bundle (the moved files plus its manifest)."""
    if not quarantine_dir.exists():
        return {"purged": False, "errors": [f"not found: {quarantine_dir}"]}
    index_entry: tuple[Path, str] | None = None
    try:
        resolved_batch = quarantine_dir.resolve()
        resolved_quarantine_root = quarantine_dir.parent.resolve()
        if (
            quarantine_dir.parent.name == "quarantine"
            and resolved_batch.parent == resolved_quarantine_root
            and resolved_batch != resolved_quarantine_root
        ):
            index_entry = (resolved_quarantine_root.parent, quarantine_dir.name)
    except (OSError, RuntimeError):
        pass
    try:
        shutil.rmtree(quarantine_dir)
        if index_entry is not None:
            _remove_quarantine_index_batch(*index_entry)
        return {"purged": True, "errors": []}
    except OSError as exc:
        return {"purged": False, "errors": [str(exc)]}


def command_restore_quarantine(args: argparse.Namespace) -> int:
    quarantine_dir = Path(args.quarantine_dir)
    if not quarantine_dir.exists():
        print(f"Quarantine dir not found: {quarantine_dir}", file=sys.stderr)
        return 2
    result = restore_quarantine_dir(quarantine_dir)
    print(f"Restored: {len(result['restored'])}")
    print(f"Skipped (source already exists): {len(result['skipped'])}")
    print(f"Errors: {len(result['errors'])}")
    for path in result["restored"]:
        print(f"  restored: {path}")
    for path in result["skipped"]:
        print(f"  skipped:  {path}")
    for err in result["errors"]:
        print(f"  error:    {err}", file=sys.stderr)
    return 0 if not result["errors"] else 1


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
        "account_owner_hint": item.get("account_owner_hint") or "unknown",
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
        category = item.get("category")
        recommendation = item.get("recommendation") or "review_required"
        action["title"] = str(path)
        action["category"] = category
        action["category_label"] = item.get("category_label")
        action["recommendation"] = recommendation
        if recommendation == "recommend_cleanup":
            action["risk"] = "low_to_medium"
            action["cleanup_target_path"] = recommended_cleanup_target(item)
            action["manual_steps"] = [
                "Close the related application first.",
                "This path looks like temporary/cache data and is generally safe to remove after review.",
                "Prefer deleting the parent cache item through the application if it provides a cleanup option.",
            ]
        else:
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
        action["category"] = "chat_data"
        action["category_label"] = "聊天数据位置"
        action["recommendation"] = "chat_account_review"
        action["chat_cleanup_modes"] = [
            {
                "mode": "personal_account",
                "label": "个人账号：先加密备份，再清理公司电脑本地残留",
                "steps": [
                    "Confirm this is a personal account and not a company/customer communication archive.",
                    "Use the app's official export/backup feature if available.",
                    "Encrypt the backup before uploading to personal cloud storage.",
                    "After verifying the backup, logout and clean local cache/history from the company computer.",
                ],
            },
            {
                "mode": "company_account",
                "label": "公司账号：不备份内容，只走交接/归档后清本地残留",
                "steps": [
                    "Do not upload company/customer chat contents to personal cloud storage.",
                    "Follow company handover or compliance archival process.",
                    "Logout from the app and clean local cache/residue after handover is complete.",
                ],
            },
            {
                "mode": "unknown",
                "label": "不确定：先人工确认账号归属",
                "steps": [
                    "Open the app and confirm whether the logged-in account is personal or company-owned.",
                    "Do not delete or upload the data directory until account ownership is clear.",
                ],
            },
        ]
        action["manual_steps"] = [
            f"Open {app} and use its official logout or cache cleanup option first.",
            "Confirm whether chat history belongs to a company account or personal account.",
            "Do not delete the data directory directly unless the app is closed and you have confirmed the account scope.",
        ]
        owner_hint = str(item.get("account_owner_hint") or "unknown")
        if owner_hint == "company_account":
            action["manual_steps"].insert(0, "Account ownership: company. Follow company handover/archival procedure, log out, then clear cache.")
        elif owner_hint == "personal_account":
            action["manual_steps"].insert(0, "Account ownership: personal. Encrypt and back up to your personal cloud first, then clean local residue.")
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

    if item_type == "ide_recent_project":
        ide = item.get("ide") or "IDE"
        project_name = item.get("name") or "unknown project"
        project_path = item.get("path") or ""
        action["title"] = f"{ide}: {project_name}"
        action["risk"] = "review_required"
        action["manual_steps"] = [
            f"Open {ide} and decide whether the project `{project_name}` is work-related.",
            "If it belongs to the company, hand the project over or move it into the company code repository before leaving.",
            "If it is personal, leave it on your own device; do not copy company source code into personal storage.",
            "Close the project in the IDE and clear the recent-projects list only after confirming the project location.",
        ]
        if project_path:
            action["related_paths"] = [project_path]
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
        if action.get("category_label"):
            lines.append(f"- Category: `{action.get('category_label')}`")
        if action.get("recommendation"):
            lines.append(f"- Recommendation: `{action.get('recommendation')}`")
        if action.get("account_owner_hint"):
            lines.append(f"- Account owner hint: `{action.get('account_owner_hint')}`")
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
        if action.get("cleanup_target_path"):
            lines.append(f"- Cleanup target path: `{action.get('cleanup_target_path')}`")
        detected_kinds = action.get("detected_secret_kinds") or []
        if detected_kinds:
            lines.append("- Detected secret kinds:")
            for kind in detected_kinds:
                lines.append(f"  - `{kind}`")
        chat_modes = action.get("chat_cleanup_modes") or []
        if chat_modes:
            lines.append("- Chat cleanup modes:")
            for mode in chat_modes:
                lines.append(f"  - `{mode.get('mode')}` {mode.get('label')}")
                for step in mode.get("steps", []):
                    lines.append(f"    - {step}")
        lines.append("")
    return "\n".join(lines)


def command_watch_install(args: argparse.Namespace) -> int:
    state_dir = state_dir_from_arg(args.state_dir, portable=bool(getattr(args, "portable", False)))
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument(
        "--state-dir",
        help="Directory that contains the .offboard-assistant state folder. Default: %%APPDATA%%\\OffboardAssistant.",
    )
    parser.add_argument(
        "--config",
        help="Path to local config.json. Defaults to <state-dir>/config.json. Never synced to cloud.",
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        help="Store state next to the running executable (in .offboard_data/). "
             "Useful for running from a USB stick or a sandboxed folder; "
             "no APPDATA writes.",
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

    restore_parser = subcommands.add_parser(
        "restore-quarantine",
        help="Restore files from a previous quarantine batch back to their original paths.",
    )
    restore_parser.add_argument(
        "--quarantine-dir",
        required=True,
        help="Path to a <state-dir>/quarantine/<timestamp>/ directory produced by the GUI.",
    )
    restore_parser.set_defaults(func=command_restore_quarantine)

    list_rules_parser = subcommands.add_parser(
        "list-rules",
        help="Print the currently active path / SaaS / secret rules.",
    )
    list_rules_parser.set_defaults(func=command_list_rules)
    return parser


def _load_config_for_args(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    config_path = Path(args.config) if getattr(args, "config", None) else state_dir / CONFIG_FILE
    if not config_path.is_absolute() and config_path.parent == Path("."):
        # Caller passed a relative bare filename — treat as under state_dir.
        config_path = state_dir / config_path
    if config_path == state_dir / CONFIG_FILE:
        return load_local_config(state_dir)
    try:
        data = read_json(config_path)
    except (OSError, ValueError):
        return default_config()
    merged = default_config()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in merged and isinstance(value, type(merged[key])):
                merged[key] = value
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
