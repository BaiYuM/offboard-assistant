# Release Checklist

Offboard Assistant publishes a complete Windows one-dir archive. Do not attach
`OffboardAssistant.exe` by itself: the executable requires the adjacent
`_internal` directory.

## 1. Prepare the release

1. Set `offboard_assistant.APP_VERSION` to the release version, for example
   `1.0.1`.
2. Update `CHANGELOG.md` and commit all intended release changes.
3. Confirm the worktree is clean and run the checks:

```powershell
git status --short
python -m unittest
python -m py_compile offboard_assistant.py offboard_gui.py offboard_gui_widgets.py ai_reviewer.py sync_bundle.py
```

## 2. Build locally

The build script installs packaging dependencies unless `-SkipInstall` is
specified. It includes `README.md` and the complete `rules` directory.

```powershell
.\build_exe.ps1
```

For an environment where `requirements-packaging.txt` is already installed:

```powershell
.\build_exe.ps1 -SkipInstall
```

For version `1.0.1`, the outputs are:

```text
dist\OffboardAssistant\
release\OffboardAssistant-windows-x64-v1.0.1.zip
release\OffboardAssistant-windows-x64-v1.0.1.zip.sha256
```

Verify the archive hash and test the extracted directory, not only the EXE:

```powershell
certutil -hashfile .\release\OffboardAssistant-windows-x64-v1.0.1.zip SHA256
Get-Content .\release\OffboardAssistant-windows-x64-v1.0.1.zip.sha256
Expand-Archive .\release\OffboardAssistant-windows-x64-v1.0.1.zip .\release\smoke-test
.\release\smoke-test\OffboardAssistant\OffboardAssistant.exe --version
```

## 3. Publish through GitHub Actions

The release tag must be exactly `v<APP_VERSION>`. The `build-windows` workflow
runs tests and compile checks, builds the archive, uploads the ZIP and SHA-256
as a workflow artifact, then creates a GitHub Release for tag builds.

```powershell
git tag -a v1.0.1 -m "Offboard Assistant v1.0.1"
git push origin v1.0.1
```

A mismatched tag, failed test, failed build, or missing artifact stops the
Release step. Re-running a successful tag workflow replaces the two Release
assets instead of creating a duplicate Release.

## 4. Privacy check

Do not publish:

- `.offboard-assistant/`
- `config.json`
- `*.enc`
- `.env` or `.env.*`
- `*.key` or `*.pem`
- `build/`, `dist/`, `build-next/`, `dist-next/`, or ad-hoc test reports
- local reports containing real paths that should not be public

Only the two generated files under `release/` are intended as Release assets.
