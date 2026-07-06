$ErrorActionPreference = "Stop"

python -m pip install -r requirements-packaging.txt
python -m PyInstaller `
  --noconfirm `
  --windowed `
  --name OffboardAssistant `
  --add-data "README.md;." `
  offboard_gui.py

Write-Host "Built: dist\OffboardAssistant\OffboardAssistant.exe"
