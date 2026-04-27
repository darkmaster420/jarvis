[CmdletBinding()]
param(
    [string]$AppRoot = "",
    [string]$PythonVersion = "3.11.9"
)

$ErrorActionPreference = "Stop"

if (-not $AppRoot) {
    $AppRoot = Split-Path -Parent $PSScriptRoot
}
$AppRoot = (Resolve-Path $AppRoot).Path
$BackendDir = Join-Path $AppRoot "backend"
$VenvDir = Join-Path $BackendDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $BackendDir "requirements.txt"
$InstallDir = Join-Path $AppRoot "_install"
$LogPath = Join-Path $AppRoot "setup_runtime.log"

function Write-SetupLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Host $Message
}

function Set-HighPerformanceGpuPreference {
    param([string[]]$ExePaths)
    $key = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
    try {
        New-Item -Path $key -Force | Out-Null
        foreach ($exe in $ExePaths) {
            if (-not $exe) { continue }
            New-ItemProperty -Path $key -Name $exe -Value "GpuPreference=2;" `
                -PropertyType String -Force | Out-Null
            Write-SetupLog "Set high-performance GPU preference for $exe"
        }
    } catch {
        Write-SetupLog "Warning: could not set GPU preference: $($_.Exception.Message)"
    }
}

function Test-JarvisRuntime {
    if (-not (Test-Path $VenvPython)) { return $false }
    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $BackendDir
    & $VenvPython -c "import jarvis, yaml, websockets, numpy; print('ok')" *> $null
    $ok = ($LASTEXITCODE -eq 0)
    $env:PYTHONPATH = $oldPythonPath
    return $ok
}

function Find-Python {
    $known = @(
        "C:\Program Files\Python311\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe"
    )
    foreach ($path in $known) {
        if (Test-Path $path) {
            & $path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) { return @($path) }
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $src = $python.Source
        if ($src -notmatch "\\msys64\\" -and $src -notmatch "\\cygwin") {
            & $src -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) { return @($src) }
        }
    }
    return @()
}

function Install-BundledPython {
    $installer = Join-Path $InstallDir ("python-{0}-amd64.exe" -f $PythonVersion)
    if (-not (Test-Path $installer)) {
        throw "Bundled Python installer not found: $installer"
    }
    Write-SetupLog "Installing Python $PythonVersion for Jarvis runtime..."
    $args = "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=1"
    $p = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        throw "Python installer failed with exit code $($p.ExitCode)"
    }
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

Write-SetupLog "Preparing Jarvis runtime in $AppRoot"
Set-HighPerformanceGpuPreference @(
    (Join-Path $AppRoot "Jarvis.exe"),
    (Join-Path $env:LocalAppData "Programs\Ollama\ollama app.exe"),
    (Join-Path $env:LocalAppData "Programs\Ollama\ollama.exe")
)
if (Test-JarvisRuntime) {
    Write-SetupLog "Jarvis runtime already works."
    exit 0
}

$pythonCmd = Find-Python
if (-not $pythonCmd -or $pythonCmd.Count -eq 0) {
    Install-BundledPython
    $pythonCmd = Find-Python
}
if (-not $pythonCmd -or $pythonCmd.Count -eq 0) {
    throw "Python was not found after installation."
}

if (Test-Path $VenvDir) {
    Write-SetupLog "Removing incomplete runtime venv..."
    Remove-Item -Recurse -Force $VenvDir
}

Write-SetupLog "Creating private Python venv..."
Invoke-Python -PythonCmd $pythonCmd -CommandArgs @("-m", "venv", $VenvDir)

Write-SetupLog "Installing backend requirements..."
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $VenvPython -m pip install --disable-pip-version-check -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "pip install requirements failed" }

Write-SetupLog "Ensuring wake word model files..."
& $VenvPython -c "from openwakeword.utils import download_models; download_models()"
if ($LASTEXITCODE -ne 0) { Write-SetupLog "Warning: openWakeWord model download failed; Jarvis will retry on startup." }

if (Test-JarvisRuntime) {
    Write-SetupLog "Jarvis runtime is ready."
    exit 0
}
throw "Jarvis runtime validation failed after install."
