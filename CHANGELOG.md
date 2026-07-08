# Changelog

All notable changes to Offboard Assistant are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com) and the
project does not yet follow Semantic Versioning (no public releases tagged).

<!-- CI-MARKER: phase1-complete -->

## Phase 1 (completed)

**Privacy-preserving cleanup assistant with first-run wizard, three-column
dashboard, AI feedback loop, and JetBrains IDE recent-project awareness.**

### Added

- `account_owner_hint` on every item (`company_account` / `personal_account` /
  `unknown`). Pure local substring matching against `origin` / `path` /
  `name`; no file contents read, no network call. User-overridable via
  `<state-dir>/config.json` (`company_email_domains`, `personal_email_domains`).
  Seed list in `KNOWN_SAAS_DOMAINS` (10 vendors). GUI prefix `[公司]/[个人]/[未知]`
  and dedicated owner column in the Treeview.
- **AI feedback loop.** `ai_review_payload_for_items(items, state_dir=...)`
  reads `handled-items.json` and emits `payload["user_feedback.handled_items"]`
  with a strict three-key whitelist `{id, type, handled_at}`. The AI prompt
  now includes a "Handled items (do NOT re-select)" block; `normalize_review_result`
  filters handled ids out of both `selected_ids` and `decisions` even if the
  model returns them.
- **IDE recent projects** via `scan_recent_ide_projects()`. Parses ONLY
  JetBrains `recentProjects.xml` (plaintext XML). Strictly avoids `.vscdb` /
  `workspaceStorage/<hash>/workspace.json` `settings` subtree / `argv.json`.
  Privacy gate covered by `test_scan_recent_ide_projects_does_not_touch_vscdb_or_workspace_settings`.
- **Three-column dashboard.** Filter sidebar (multi-select `category` /
  `recommendation` / `owner` / `confidence`) + Treeview (with new owner
  column) + right-hand `DetailPanel` showing full item JSON + per-item
  `隔离此项` / `标记已处理` / `复制路径` buttons.
- **FirstRunWizard.** Three-step `ttk.Notebook` dialog (baseline date /
  company & personal email domains / scan roots). Writes `config.json` +
  `wizard.done`; re-surfaces only when both baseline and marker are missing.
- **Toolbar refactor + OverflowMenu.** 11 buttons split into 3 visible
  groups + a right-side `ttk.Menubutton` overflow holding destructive
  actions (`隔离选中推荐项` / `标记选中已处理` / `查看隔离历史` / `AI 审核:*` / `清空勾选`).
- **Status bar with `StatusLevel` enum** (info / warn / error / busy). 22
  call sites re-classified. Messagebox stays modal.
- **Quarantine is now reversible.** `restore_quarantine_dir(quarantine_dir)`
  + `purge_quarantine_dir(quarantine_dir)` + `list_quarantine_bundles(state_dir)`
  in core. CLI subcommand `offboard_assistant.py restore-quarantine
  --quarantine-dir <path>`. GUI entry: `更多操作 ▾` → `查看隔离历史`.
- **`config.json` local-only preferences** (scan-roots, company/personal
  domains, IDE scan toggle). CLI `--config <path>` override. **Never added
  to `sync_bundle.SYNC_FILES`**; `.gitignore` excludes it.
- **`offboard_gui_widgets.py`** — the four heavy widget classes
  (`StatusLevel` / `FirstRunWizard` / `OverflowMenu` / `FilterSidebar` /
  `DetailPanel` + two helper functions) moved out of the main GUI file so
  the controller class can be read top-to-bottom without scrolling.

### Changed

- `describe_item` no longer dumps raw item JSON in the unknown-type branch
  (would have leaked `secret_findings.masked` and `username_masked`).
  Fallback is now `f"unsupported type: {item_type}"`.
- `cleanup_action_for_item` carries `account_owner_hint` and adds
  ownership-aware first steps for chat directories. The markdown plan
  renders an `Account owner hint` line per action.
- `ai_review_payload_for_items` now injects `account_owner_hint` per item
  and (when `state_dir` is provided) reads `handled-items.json` for the
  feedback section.
- `init` / `scan` / `report` / `actions` all consume `--config` and merge
  `config.json` `scan_roots` via `resolve_scan_roots` (CLI + config +
  default fallback).

### Tests

- **60 unit + e2e tests**, all green on Windows; cross-platform-friendly.
  New: 3 IDE scanner tests with privacy gates, 1 AI feedback exclude test,
  2 config / wizard / scan-roots tests, 5 quarantine-restore tests
  (including 1 CLI subcommand roundtrip), 3 status-level / DetailPanel
  routing tests.
- **`tests/e2e/test_cli_workflow.py`** — subprocess-based workflow tests
  that spawn `offboard_assistant.py` against a tempdir, exercising the
  full `init → scan → report → actions` chain and asserting no secret
  values appear in the on-disk JSON. Discovered by the existing
  `python -m unittest` invocation; no CI workflow change.

### Hard rules preserved

- `SECURITY.md` core 5 rules unchanged.
- `sync_bundle` encryption protocol (PBKDF2-HMAC-SHA256 + Fernet) unchanged.
- `SYNC_FILES` allowlist unchanged; `config.json` deliberately excluded.
- 0 new pip dependencies.
