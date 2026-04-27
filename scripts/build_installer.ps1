[CmdletBinding()]
param(
    [switch]$SkipPackage,
    # Skip deleting dist\Jarvis first — use when Remove-Item fails (file lock on backend\.venv, etc.)
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Iss  = Join-Path $Root "installer\jarvis.iss"
$Staged = Join-Path $Root "dist\Jarvis"
$PackageScript = Join-Path $Root "scripts\package_windows.ps1"

if (-not $SkipPackage) {
    Write-Host "==> Staging dist\Jarvis (scripts\package_windows.ps1)" -ForegroundColor Cyan
    if ($NoClean) {
        & $PackageScript -NoClean
    } else {
        try {
            & $PackageScript
        } catch {
            Write-Host "==> Retrying with -NoClean (dist\Jarvis is often locked by Python)..." -ForegroundColor Yellow
            & $PackageScript -NoClean
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "package_windows.ps1 failed (exit $LASTEXITCODE)" }
}

if (-not (Test-Path $Staged)) {
    throw "dist\Jarvis not found. Run without -SkipPackage or run scripts\package_windows.ps1 first."
}
if (-not (Test-Path (Join-Path $Staged "Jarvis.exe"))) {
    throw "dist\Jarvis\Jarvis.exe missing. Run package_windows.ps1 (builds the HUD)."
}

$iscc = $null
$localInno = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
foreach ($c in @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        $localInno
    )) {
    if (Test-Path $c) { $iscc = $c; break }
}
if (-not $iscc) {
    throw "ISCC.exe not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php"
}

Write-Host "==> Compiling installer ($iscc)" -ForegroundColor Cyan
& $iscc $Iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

$outDir = Join-Path $Root "dist\installer"
Write-Host ""
Write-Host "Installer output: $outDir" -ForegroundColor Green
