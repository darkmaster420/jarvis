[CmdletBinding()]
param(
    [string]$Generator = "",
    [string]$Configuration = "Debug",
    [double]$ReloadInterval = 0.8,
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FrontendDir = Join-Path $Root "frontend"
$BuildDir = Join-Path $FrontendDir "build-debug-hud"

function Stop-JarvisBackendProcesses {
    param([string]$RepoRoot)
    $rootNorm = ($RepoRoot -replace "\\", "/").ToLowerInvariant()
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $cmd = ($_.CommandLine | Out-String).Trim()
            if (-not $cmd) { return $false }
            $cmdNorm = ($cmd -replace "\\", "/").ToLowerInvariant()
            return (
                $cmdNorm -match "(^|\s)-m\s+jarvis\.main(\s|$)" -or
                $cmdNorm -match "jarvis/main\.py"
            ) -and (
                $cmdNorm.Contains($rootNorm) -or
                $cmdNorm.Contains("/jarvis/backend/")
            )
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Resolve-BuiltExe {
    param([string]$BuildRoot, [string]$Config, [string]$ExeName)
    $cfgDir = Join-Path $BuildRoot $Config
    $p = Join-Path $cfgDir $ExeName
    if (Test-Path $p) { return $p }
    $p2 = Join-Path $BuildRoot $ExeName
    if (Test-Path $p2) { return $p2 }
    return $null
}

Push-Location $Root
try {
    # Avoid LNK1168/file-lock errors when rebuilding while HUD is running.
    Get-Process -Name "jarvis-debug", "jarvis" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Stop-JarvisBackendProcesses -RepoRoot $Root

    if (-not $NoBuild) {
        $cfgArgs = @("-S", $FrontendDir, "-B", $BuildDir)
        if ($Generator) {
            $cfgArgs += @("-G", $Generator)
        }
        cmake @cfgArgs
        if ($LASTEXITCODE -ne 0) { throw "CMake configure failed." }

        cmake --build $BuildDir --config $Configuration --target jarvis_debug_hud
        if ($LASTEXITCODE -ne 0) { throw "Debug HUD build failed." }
    }

    $ExePath = Resolve-BuiltExe $BuildDir $Configuration "jarvis-debug.exe"
    if (-not $ExePath) {
        throw "Missing jarvis-debug.exe under $BuildDir (expected $Configuration\jarvis-debug.exe or flat layout). Build first or run without -NoBuild."
    }

    Write-Host "Launching debug HUD (console) + backend with DEBUG logs..."
    $exeArgs = @("--reload-interval", "$ReloadInterval")
    $hudProc = Start-Process -FilePath $ExePath -ArgumentList $exeArgs -PassThru
    try {
        Wait-Process -Id $hudProc.Id
    }
    finally {
        # If HUD exits or crashes, ensure orphaned backend workers are cleaned up.
        Stop-JarvisBackendProcesses -RepoRoot $Root
    }
}
finally {
    Pop-Location
}
