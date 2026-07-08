$ErrorActionPreference = "Stop"

param(
    [string]$Key = "",
    [switch]$NoConfirm = $false
)

# Install build deps. `--key` encryption requires the optional `pyinstaller[encryption]`
# extra which pulls in `tinyaes`. If you don't pass -Key, the basic pyinstaller + cryptography
# requirements are enough.
python -m pip install -r requirements-packaging.txt

$pyinstallerArgs = @(
    "--noconfirm"
    "--windowed"
    "--name", "OffboardAssistant"
    "--add-data", "README.md;."
)

if ($Key) {
    if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
        Write-Host "PyInstaller is required for --key encryption. Install with: pip install 'pyinstaller[encryption]'"
        exit 1
    }
    Write-Host "Building with bootloader encryption (key fingerprint: $([System.BitConverter]::ToString(([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Key)))) -replace '-', ''))..."
    $pyinstallerArgs += @("--key", $Key)
} else {
    Write-Host "Building without bootloader encryption. Pass -Key <passphrase> to enable (requires 'pyinstaller[encryption]')."
}

$pyinstallerArgs += "offboard_gui.py"

python -m PyInstaller @pyinstallerArgs

Write-Host "Built: dist\OffboardAssistant\OffboardAssistant.exe"