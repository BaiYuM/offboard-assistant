[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildDir = Join-Path $repoRoot "build"
$distDir = Join-Path $repoRoot "dist"
$releaseDir = Join-Path $repoRoot "release"
$appDir = Join-Path $distDir "OffboardAssistant"

Push-Location $repoRoot
try {
    if (-not $SkipInstall) {
        python -m pip install -r requirements-packaging.txt
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install packaging dependencies."
        }
    }

    $versionOutput = & python -c "import offboard_assistant as core; print(core.APP_VERSION)"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read offboard_assistant.APP_VERSION."
    }
    $version = "$versionOutput".Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
        throw "offboard_assistant.APP_VERSION must be a valid release version, got '$version'."
    }

    $artifactName = "OffboardAssistant-windows-x64-v$version"
    $zipPath = Join-Path $releaseDir "$artifactName.zip"
    $hashPath = "$zipPath.sha256"

    New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
    if (Test-Path -LiteralPath $appDir) {
        Remove-Item -LiteralPath $appDir -Recurse -Force
    }
    foreach ($path in @($zipPath, $hashPath)) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }

    $pyinstallerArgs = @(
        "--noconfirm"
        "--clean"
        "--windowed"
        "--name", "OffboardAssistant"
        "--distpath", $distDir
        "--workpath", $buildDir
        "--specpath", $buildDir
        "--add-data", "$(Join-Path $repoRoot 'README.md');."
        "--add-data", "$(Join-Path $repoRoot 'rules');rules"
        (Join-Path $repoRoot "offboard_gui.py")
    )

    python -m PyInstaller @pyinstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }

    $requiredOutputs = @(
        (Join-Path $appDir "OffboardAssistant.exe")
        (Join-Path $appDir "_internal\README.md")
        (Join-Path $appDir "_internal\rules\default.yaml")
    )
    foreach ($path in $requiredOutputs) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required packaged file is missing: $path"
        }
    }

    Compress-Archive -LiteralPath $appDir -DestinationPath $zipPath -CompressionLevel Optimal
    $stream = [IO.File]::OpenRead($zipPath)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
    Set-Content -LiteralPath $hashPath -Value "$hash  $([IO.Path]::GetFileName($zipPath))" -Encoding Ascii

    Write-Host "Built directory: $appDir"
    Write-Host "Release archive: $zipPath"
    Write-Host "SHA-256 file: $hashPath"
}
finally {
    Pop-Location
}
