[CmdletBinding()]
param(
    [string]$Configuration = "Release",
    [string]$PythonVersion = "3.11.9",
    [string]$PythonExe = "",
    [switch]$NoClean,
    [switch]$SkipVenv,
    [switch]$SkipPythonInstaller,
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistRoot = Join-Path $Root "dist"
$AppDir = Join-Path $DistRoot "Jarvis"
$BackendStage = Join-Path $AppDir "backend"
$InstallStage = Join-Path $AppDir "_install"
$FrontendBuild = Join-Path $Root "frontend\build"
$JarvisExe = Join-Path $FrontendBuild "jarvis.exe"

function Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Copy-Tree {
    param([string]$Source, [string]$Dest)
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
    Copy-Item -Recurse -Force $Source $Dest
}

function Resolve-Python {
    if ($PythonExe) {
        if (-not (Test-Path $PythonExe)) { throw "PythonExe not found: $PythonExe" }
        return @($PythonExe)
    }
    # Prefer a real/base Python for creating a redistributable venv. Using the
    # project venv's python to create another venv can hang or inherit odd paths.
    $known = @(
        "C:\Program Files\Python311\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe"
    )
    foreach ($path in $known) {
        if (Test-Path $path) {
            & $path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -eq 0) { return @($path) }
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $src = $python.Source
        if ($src -notmatch "\\msys64\\" -and $src -notmatch "\\cygwin") {
            & $src -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -eq 0) { return @($src) }
        }
    }
    $venvPy = Join-Path $Root "backend\.venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return @($venvPy) }
    throw "No Python found. Install Python 3.11+ on the packaging machine or pass -PythonExe."
}

function Invoke-Python {
    param([string[]]$PythonCmd, [string[]]$CommandArgs)
    if ($PythonCmd.Count -eq 2) {
        & $PythonCmd[0] $PythonCmd[1] @CommandArgs
    } else {
        & $PythonCmd[0] @CommandArgs
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $($PythonCmd -join ' ') $($CommandArgs -join ' ')"
    }
}

function Write-SanitizedConfig {
    $src = Join-Path $Root "config.default.yaml"
    if (-not (Test-Path $src)) { throw "Missing $src - add config.default.yaml to the repo." }
    $dst = Join-Path $AppDir "config.default.yaml"
    Copy-Item -Force $src $dst
}

function Remove-GeneratedPythonFiles {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    Get-ChildItem -Path $Path -Directory -Recurse -Force -Filter "__pycache__" |
        Remove-Item -Recurse -Force
    Get-ChildItem -Path $Path -File -Recurse -Force -Include "*.pyc", "*.pyo" |
        Remove-Item -Force
}

Step "Preparing dist folder"
if ((Test-Path $AppDir) -and -not $NoClean) {
    Remove-Item -Recurse -Force $AppDir
}
New-Item -ItemType Directory -Force -Path $AppDir, $BackendStage, $InstallStage | Out-Null

Step "Building frontend"
if (-not $SkipFrontendBuild) {
    $ucrt = "C:\msys64\ucrt64\bin"
    if (Test-Path $ucrt) { $env:PATH = "$ucrt;$env:PATH" }
    # Reconfigure every time: CMake cache (e.g. JARVIS_HUD_VERSION) must not stay stale.
    cmake -S (Join-Path $Root "frontend") -B $FrontendBuild
    if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }
    Get-Process jarvis -ErrorAction SilentlyContinue | Stop-Process -Force
    cmake --build $FrontendBuild --config $Configuration
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
}
if (-not (Test-Path $JarvisExe)) { throw "Frontend exe not found: $JarvisExe" }
Copy-Item -Force $JarvisExe (Join-Path $AppDir "Jarvis.exe")

Step "Staging backend and config"
Copy-Tree (Join-Path $Root "backend\jarvis") (Join-Path $BackendStage "jarvis")
Remove-GeneratedPythonFiles (Join-Path $BackendStage "jarvis")
Copy-Item -Force (Join-Path $Root "backend\requirements.txt") (Join-Path $BackendStage "requirements.txt")
Write-SanitizedConfig
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $AppDir "backend.log")
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $AppDir "setup_runtime.log")

foreach ($dir in @("profiles", "memory", "proposed_patches", "user_skills")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $AppDir $dir) | Out-Null
}
if (Test-Path (Join-Path $Root "user_skills\.gitkeep")) {
    Copy-Item -Force (Join-Path $Root "user_skills\.gitkeep") (Join-Path $AppDir "user_skills\.gitkeep")
}

$modelsSrc = Join-Path $Root "models"
if (Test-Path $modelsSrc) {
    Step "Staging local models"
    Copy-Tree $modelsSrc (Join-Path $AppDir "models")
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "models") | Out-Null
}

Step "Staging runtime setup helper"
Copy-Item -Force (Join-Path $Root "scripts\setup_runtime.ps1") (Join-Path $InstallStage "setup_runtime.ps1")

if (-not $SkipPythonInstaller) {
    Step "Ensuring bundled Python installer"
    $pyInstaller = Join-Path $InstallStage ("python-{0}-amd64.exe" -f $PythonVersion)
    if (-not (Test-Path $pyInstaller)) {
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
        Write-Host "Downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $pyInstaller
    }
}

if (-not $SkipVenv) {
    Step "Creating staged private backend venv"
    $pythonCmd = Resolve-Python
    $venvDir = Join-Path $BackendStage ".venv"
    if (Test-Path $venvDir) { Remove-Item -Recurse -Force $venvDir }
    Invoke-Python -PythonCmd $pythonCmd -CommandArgs @("-m", "venv", $venvDir)
    $venvPy = Join-Path $venvDir "Scripts\python.exe"
    & $venvPy -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    & $venvPy -m pip install --disable-pip-version-check -r (Join-Path $BackendStage "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install requirements failed" }
    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $BackendStage
    & $venvPy -c "import jarvis, yaml, websockets, numpy; print('runtime ok')"
    $env:PYTHONPATH = $oldPythonPath
    if ($LASTEXITCODE -ne 0) { throw "staged runtime validation failed" }
}

Step "Writing package README"
@'
Jarvis Windows package
======================

Run Jarvis.exe to start the HUD and backend.

When installed under Program Files, the HUD window position is saved as
%LocalAppData%\Jarvis\hud_layout.json (a copy from the old exe folder is used
once if present). In portable/dev trees it stays next to Jarvis.exe.

If the backend runtime is missing or broken, run:
  powershell -ExecutionPolicy Bypass -File .\_install\setup_runtime.ps1

Ollama must be installed/running for LLM and vision features. Jarvis will
auto-pull configured Ollama models on first run when Ollama is reachable.

To build the redistributable installer (Inno Setup 6 required), from the repo:
  powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
Output: dist\installer\JarvisSetup-<version>.exe
'@ | Set-Content -Path (Join-Path $AppDir "README.txt") -Encoding UTF8

Write-Host ""
Write-Host "Packaged Jarvis at: $AppDir" -ForegroundColor Green
