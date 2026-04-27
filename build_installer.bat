@echo off
REM One-step: rebuild HUD, stage dist\Jarvis, compile JarvisSetup-*.exe
REM Pass-through:  -SkipPackage  (Inno only)   -NoClean  (always skip deleting dist\Jarvis first)
REM Bump installer\jarvis.iss + frontend\CMakeLists.txt (JARVIS_HUD_VERSION) for new versions.
setlocal
cd /d "%~dp0"

echo ==^> Stopping Jarvis HUD if running (unlocks jarvis.exe)...
taskkill /IM jarvis.exe /F 2>nul

echo ==^> scripts\build_installer.ps1 (auto-retries packaging with -NoClean if dist is locked^)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_installer.ps1" %*
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
  echo.
  echo Build failed.
  exit /b %EC%
)
echo.
echo Done. dist\installer\
for %%F in ("%~dp0dist\installer\JarvisSetup-*.exe") do echo   %%~nxF
exit /b 0
