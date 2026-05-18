@echo off
REM Refresh the SAPI voice list. Run after dropping new voice folders
REM into %USERPROFILE%\Documents\Speechify\en-US\.
REM
REM Self-elevates via UAC because regsvr32 writes to HKLM. The 32-bit
REM regsvr32 (in SysWOW64 — the naming is historical) is required for
REM our 32-bit COM DLL.

setlocal

REM Self-elevate. If we're not admin, relaunch ourselves elevated.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

set "DLL=%~dp0spfy_sapi.dll"
if not exist "%DLL%" (
    echo ERROR: spfy_sapi.dll not found at %DLL%
    echo The installer may be broken — reinstall.
    pause
    exit /b 1
)

echo Refreshing Speechify SAPI voice list...
echo   DLL: %DLL%
echo   Scanning %USERPROFILE%\Documents\Speechify\en-US\
echo.

REM /u first to clear any stale tokens, then re-register
"%SystemRoot%\SysWOW64\regsvr32.exe" /s /u "%DLL%"
"%SystemRoot%\SysWOW64\regsvr32.exe" /s "%DLL%"
if %errorlevel% neq 0 (
    echo ERROR: regsvr32 returned %errorlevel%
    echo Voices may not be available in SAPI clients.
    pause
    exit /b 1
)

echo Done. Restart your SAPI client (Balabolka, Narrator, etc.) to see new voices.
echo.
pause
