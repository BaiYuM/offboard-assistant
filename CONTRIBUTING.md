# Contributing

## Development Setup

Use Python 3.11 or newer on Windows.

```powershell
python -m pip install -r requirements-packaging.txt
python -m unittest
```

Run the CLI:

```powershell
python .\offboard_assistant.py --help
```

Run the GUI:

```powershell
python .\offboard_gui.py
```

Build the EXE:

```powershell
.\build_exe.ps1
```

## Rules for Changes

- Do not add code that records plaintext passwords, token values, cookies, or chat contents.
- Do not add keylogging, browser input interception, process injection, or driver-level monitoring.
- Default behavior must be review-first, not delete-first.
- Cloud sync must upload encrypted bundles only.
- Add tests for new parsing, diffing, sync, and action-generation logic.

## Before Opening a PR

```powershell
python -m unittest
python -m py_compile offboard_assistant.py offboard_gui.py sync_bundle.py
```

