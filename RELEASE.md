# Release Checklist

1. Run tests.

```powershell
python -m unittest
python -m py_compile offboard_assistant.py offboard_gui.py sync_bundle.py
```

2. Build the Windows EXE.

```powershell
.\build_exe.ps1
```

3. Verify the EXE.

```powershell
.\dist\OffboardAssistant\OffboardAssistant.exe --help
.\dist\OffboardAssistant\OffboardAssistant.exe --state-dir . --background-scan
```

4. Ensure private/local data is not included.

Do not publish:

- `.offboard-assistant/`
- `*.enc`
- `.env`
- `build/`
- Local test reports containing real paths you do not want public.

5. Attach the built EXE to a GitHub Release if desired.

