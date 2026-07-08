# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Offboard Assistant — a Windows-first, local-first, **privacy-preserving** onboarding/offboarding cleanup helper. It records *metadata* (paths, masked usernames, timestamps, secret types) about things installed/added after a baseline date so a user can later clean up work residue. It deliberately **never** reads, stores, or transmits passwords, chat contents, cookies, or full API key/token values.

Targets Python 3.11+ on Windows. Pure-stdlib code; only `cryptography` (for Fernet sync bundles) and `pyinstaller` (for EXE) are optional extras.

## Common commands

All commands are PowerShell / bash on Windows. State directory defaults to `%APPDATA%\OffboardAssistant\.offboard-assistant\` (the legacy `.offboard-assistant/` in cwd is auto-migrated on first run).

```powershell
# Install packaging deps (PyInstaller + cryptography)
python -m pip install -r requirements-packaging.txt

# Run the full test suite (only dependency-free tests; cryptography is optional)
python -m unittest
python -m unittest test_offboard_assistant.OffboardAssistantTests.test_mask_identifier_email   # single test example

# Byte-compile the three production modules (used by CI as a smoke check)
python -m py_compile offboard_assistant.py offboard_gui.py sync_bundle.py

# CLI subcommands (subcommand required: init | scan | report | actions | watch-install)
python .\offboard_assistant.py init --since 2026-07-06 --scan-root E:\job
python .\offboard_assistant.py scan
python .\offboard_assistant.py report --rescan --csv .\.offboard-assistant\offboarding-report.csv
python .\offboard_assistant.py actions --rescan --output .\.offboard-assistant\cleanup-actions.md
python .\offboard_assistant.py watch-install --once
python .\offboard_assistant.py watch-install --interval 60

# GUI
python .\offboard_gui.py

# Build the windowed EXE -> dist\OffboardAssistant\OffboardAssistant.exe
.\build_exe.ps1
# equivalent manual invocation:
python -m PyInstaller --noconfirm --windowed --name OffboardAssistant --add-data "README.md;." offboard_gui.py
```

CI (`.github/workflows/test.yml`) runs `python -m unittest` + `py_compile` on `windows-latest`, Python 3.12. `.github/workflows/build-windows.yml` builds the EXE on tag push or manual dispatch.

## Code architecture

Four small Python files, no package, no framework — they are designed to be read end-to-end. Read them in this order:

1. **`offboard_assistant.py`** (~1800 lines) — the core engine.
   - **Constants / secret-detection vocab**: `SENSITIVE_FILENAMES`, `SENSITIVE_PATTERNS`, `DEFAULT_SCAN_DIR_NAMES`, `AI_APP_CONFIG_DIR_NAMES`, `SECRET_CONTENT_EXTENSIONS`, `SECRET_PATTERNS`, `CHAT_APP_DIRS` — these drive what the scanner looks for. Change here when adding a new vendor / AI tool / chat app.
   - **Helpers**: `mask_identifier`, `mask_secret`, `secret_fingerprint` (deterministic hash, never the secret), `stable_id` (for diff identity), `categorize_path`, `recommended_cleanup_target`.
   - **Scanners** (each returns `list[dict]` items with `id`, `type`, timestamps, etc.):
     - `scan_installed_apps_from_registry` — Windows uninstall registry.
     - `scan_environment` / `scan_environment_for_install_monitor` — user + system env vars, splits `PATH`-like values.
     - `scan_browser_logins` (delegates to `scan_chromium_logins` + `scan_firefox_logins`) — copies the login DB to a temp file before reading (`copy_for_read` to avoid lock issues); records origin URL + masked username + timestamp, **never** the password field.
     - `scan_sensitive_locations` + `detect_secret_references` — walks `--scan-root`/default roots, classifies by filename/extension/pattern, and grep-scans *only* text-like configs for regex matches; results are `{secret_type, file_path, line, snippet (redacted), fingerprint}`.
     - `scan_chat_locations` — known vendor data directories (`CHAT_APP_DIRS`), paths only.
     - `install_monitor_snapshot` + `diff_install_monitor_snapshots` — lightweight fingerprint-based install watcher (top-level `Program Files`/`AppData` entries, registry, env vars, PATH entries). **Not** driver/instrumentation-based; safe and intentionally noisy.
   - **Diffing**: `diff_items` (snapshot vs baseline → `cleanup_confidence`: `high_new_after_since` | `medium_new_but_time_unknown` | `needs_review_modified_after_since` | `low`).
   - **Reporting**: `render_report`, `write_csv`, `cleanup_action_for_item` (per-item cleanup recipe: risk, manual steps, copy-pasteable PowerShell commands), `render_cleanup_actions_markdown`.
   - **CLI**: `command_init` / `command_scan` / `command_report` / `command_actions` / `command_watch_install`, wired by `build_parser` → `main`.
   - **State I/O**: `default_state_base`, `state_dir_from_arg`, `migrate_legacy_state_if_needed`, `read_json` / `write_json` / `append_jsonl` / `read_jsonl`. State filenames are the constants `BASELINE_FILE`, `SNAPSHOT_FILE`, `REPORT_FILE`, `INSTALL_MONITOR_STATE_FILE`, `INSTALL_EVENTS_FILE`.
   - The CLI is **intentionally review-first**: `actions` and `report` produce Markdown only; no command deletes files. The GUI's "quarantine" button moves files into `<state>/quarantine/` with a `manifest.json`, not a delete.

2. **`offboard_gui.py`** (~830 lines) — Tkinter `OffboardGui(tk.Tk)` with five `ttk.Notebook` tabs: 清理清单 (dashboard), 云同步/导入导出 (sync), AI 审核 (AI), 后台任务 (Windows `schtasks`), 说明 (guide). Imports `offboard_assistant as core` and reuses its scanners/diff/report — the GUI is a thin shell. Notable methods: `refresh_data`, `run_ai_review`, `export_ai_review_pack`, `quarantine_selected_recommended`, `mark_selected_handled`, `create_watch_task` / `create_daily_scan_task` (wraps `schtasks` via `run_schtasks`).

3. **`ai_reviewer.py`** — OpenAI-compatible client (`urllib` only). Three functions: `list_openai_compatible_models`, `review_with_openai_compatible`, plus `normalize_review_result` (filters AI-returned IDs against the candidate set so the model cannot invent IDs). The system prompt is hard-coded; the user prompt is JSON. Payload sent to the model is **metadata-only** — see `ai_review_payload_for_items` in `offboard_assistant.py` for what fields are exported.

4. **`sync_bundle.py`** — Fernet-encrypted bundle I/O. `SYNC_FILES` is the explicit allowlist of state files that get bundled (note: it's the only place to add a new file to sync). `_encrypt_bytes` uses PBKDF2-HMAC-SHA256 (390k iterations) + Fernet; envelope is JSON containing `salt` + `token`. WebDAV upload/download is bare `urllib.request` with Basic auth. `crypto_available()` lets the GUI degrade gracefully when `cryptography` is not installed.

5. **`test_offboard_assistant.py`** — `unittest`-only, no fixtures, no mocks framework. Covers masking, ID stability, diff confidence levels, install-monitor signal detection, AI review normalization, sync bundle round-trip, etc.

## Recent additions (Phase 1 complete)

All Phase 1 sub-tasks landed without breaking the privacy boundary. If you touch the relevant code, the tests under `tests/e2e/` and the new unit tests in `test_offboard_assistant.py` will fail loudly if you regress them.

### Data layer

- **`account_owner_hint`** on every item: `company_account` / `personal_account` / `unknown`. Inferred by `infer_account_owner_hint(item, config)` in `offboard_assistant.py:301` (pure local substring matching against `origin` / `path` / `title`; never reads file contents, never networked). The GUI's `describe_item` (`offboard_gui.py:1088`) prefixes every row with `[公司]/[个人]/[未知]`; the `cleanup_action_for_item` markdown plan renders the hint and injects ownership-aware first steps for chat directories. User-supplied company/personal domains come from `<state>/config.json` (`company_email_domains`, `personal_email_domains`); the `KNOWN_SAAS_DOMAINS` frozenset at `offboard_assistant.py:189` is the seed list.
- **IDE recent projects** via `scan_recent_ide_projects()` in `offboard_assistant.py:1220`. **Parses ONLY JetBrains `recentProjects.xml` (plaintext XML).** Strictly avoids `.vscdb` / `workspaceStorage/<hash>/workspace.json` settings subtree / `argv.json` — see `test_scan_recent_ide_projects_does_not_touch_vscdb_or_workspace_settings` for the privacy gate.
- **AI feedback loop**: `ai_review_payload_for_items(items, state_dir=...)` in `offboard_assistant.py:397` reads `handled-items.json` and emits `payload["user_feedback.handled_items"]` with a strict three-key whitelist `{id, type, handled_at}` — `title` / `path` / `origin` are explicitly dropped. `ai_reviewer.normalize_review_result(..., excluded_ids=...)` filters handled ids out even if the model returns them.

### Persistence / config

- **`config.json`** (`offboard_assistant.py:560`) — local-only user preferences (scan-roots, company/personal domains, IDE scan toggle). **Never added to SYNC_FILES**; `.gitignore` excludes it. CLI takes `--config <path>` to override location.
- **`wizard.done`** — sentinel file under `<state>/`. Written by `FirstRunWizard` so the wizard surfaces on next launch only when there's no baseline yet.
- **`resolve_scan_roots(args, config)`** — merge precedence `CLI > config > builtin default`. CLI roots come first, then config roots; if both empty, fallback to `default_scan_roots()`.

### Quarantine lifecycle

- **`restore_quarantine_dir(quarantine_dir)`** + **`purge_quarantine_dir(quarantine_dir)`** + **`list_quarantine_bundles(state_dir)`** in `offboard_assistant.py`. Reverse `quarantine_selected_recommended` by reading `manifest.json`. Conflicts (source already exists) are *skipped*, never overwritten. CLI subcommand: `offboard_assistant.py restore-quarantine --quarantine-dir <path>`. GUI entry: `更多操作 ▾` → `查看隔离历史`.

### GUI reorganization

- **`FirstRunWizard(tk.Toplevel)`** in `offboard_gui.py` — three-step `ttk.Notebook` dialog (baseline date / company & personal domains / scan roots). Skippable; can be re-triggered by deleting `wizard.done`.
- **`OverflowMenu(ttk.Menubutton)`** in `offboard_gui.py` — secondary actions live in a `tk.Menu(tearoff=0)` instead of competing for horizontal toolbar space. Dashboard toolbar is now three groups + separator + an overflow on the right.
- **`FilterSidebar`** + **`DetailPanel`** — three-column dashboard (`180px` filters | `weight=3` Treeview | `280px` details). Detail panel shows the selected item's full JSON + per-item `隔离此项` / `标记已处理` / `复制路径` buttons. `render_tree` now respects sidebar filters via `filter_sidebar.apply(candidates)`.
- **owner column** added to the Treeview (`公司/个人/未知`).

### Tests

- **End-to-end CLI tests** at `tests/e2e/test_cli_workflow.py` (7 tests, cross-platform): spawn `offboard_assistant.py` as a subprocess against a tempdir state dir, run `init → scan → report → actions`, assert no secret values appear on disk and that IDE items land in the snapshot. CI's existing `python -m unittest` discovers them automatically — no workflow change needed.
- **Total** now: **57 unit + e2e tests**, all green.

### E2E gotchas

- `--state-dir <path>` lands the actual state at `<path>/.offboard-assistant/` (see `state_dir_from_arg` + `ensure_state_dir` at `offboard_assistant.py:474-481`). The e2e tests account for this; new tests should too.
- Set `APPDATA=<tmp>` (Windows) / `XDG_CONFIG_HOME=<tmp>` (macOS/Linux) in the subprocess env to redirect IDE discovery (`scan_recent_ide_projects`) to the fixture root instead of the user's real APPDATA.

## Hard rules (from SECURITY.md / CONTRIBUTING.md)

These are repo-level invariants — do not relax them in a PR:

- **Never store or transmit plaintext secrets** — passwords, cookies, chat contents, API key/token values. Everything in snapshots/snapshots-in-AI-payloads is metadata + masked identifiers + secret fingerprints (hash).
- **Never decrypt browser password values.** Chromium/Firefox login scans only read origin URL + masked username + timestamps via a *copy* of the DB.
- **No keylogging, browser input interception, process injection, or driver-level monitoring.** Install detection is fingerprint/diff only.
- **Default behavior is review-first, not delete-first.** Quarantine (move to `<state>/quarantine/` with `manifest.json`) is the strongest automatic action in the GUI.
- **Cloud sync is encrypted-bundle only.** The WebDAV path uploads `.enc`; never raw `baseline.json` etc.
- **Tests are required** for new parsing, diffing, sync, and action-generation logic.

## Don't publish

`.gitignore` already excludes these — never commit them in any branch: `.offboard-assistant/`, `*.enc`, `.env`, `*.key`, `*.pem`, `build/`, `dist/`, `*.spec`. The `.offboard-assistant/` dir in cwd contains real local scan data and is in `.gitignore` for that reason.