@echo off
setlocal EnableDelayedExpansion

REM Speechify Voice Registration Script
REM Registers the SAPI5 voice DLL and adds registry entries for Balabolka/SAPI apps.

REM --- Check for admin privileges and self-elevate if needed ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0.' -Verb RunAs"
    exit /b 0
)

REM --- Colors: use escape character for ANSI codes ---
for /f %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"
set "GREEN=%ESC%[92m"
set "RED=%ESC%[91m"
set "YELLOW=%ESC%[93m"
set "CYAN=%ESC%[96m"
set "BOLD=%ESC%[1m"
set "RESET=%ESC%[0m"

echo.
echo %BOLD%%CYAN%========================================%RESET%
echo %BOLD%%CYAN%  Speechify Voice Registration%RESET%
echo %BOLD%%CYAN%========================================%RESET%
echo.

REM --- Locate the voice DLL ---
set "VOICE_DLL=%~dp0bin\SAPI5Speechify.dll"

if not exist "%VOICE_DLL%" (
    echo %RED%ERROR:%RESET% Voice DLL not found at:
    echo   %YELLOW%%VOICE_DLL%%RESET%
    echo.
    echo Make sure this script is in the Speechify root folder.
    pause
    exit /b 1
)

echo %GREEN%[1/3]%RESET% Found voice DLL
echo       %YELLOW%%VOICE_DLL%%RESET%
echo.

REM --- Register the voice DLL ---
echo %GREEN%[2/3]%RESET% Registering SAPI5 voice DLL...
regsvr32 /s "%VOICE_DLL%"
if errorlevel 1 (
    echo.
    echo %RED%ERROR:%RESET% DLL registration failed. Try running as Administrator.
    pause
    exit /b 1
)
echo       %GREEN%Done.%RESET%
echo.

REM --- Add registry entries ---
echo %GREEN%[3/3]%RESET% Adding registry entries...

set "REGKEY=HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom"

reg add "%REGKEY%" /ve /d "Speechify Tom" /f >nul 2>&1
reg add "%REGKEY%" /v "409" /d "Speechify Tom" /f >nul 2>&1
reg add "%REGKEY%" /v "CLSID" /d "{0215FC19-483E-4AF7-B608-1FF364DCCB56}" /f >nul 2>&1
reg add "%REGKEY%" /v "VoicePath" /d "%VOICE_DLL%" /f >nul 2>&1
reg add "%REGKEY%" /v "EnginePath" /d "%VOICE_DLL%" /f >nul 2>&1
reg add "%REGKEY%\Attributes" /v "Language" /d "409" /f >nul 2>&1
reg add "%REGKEY%\Attributes" /v "Name" /d "Speechify Tom" /f >nul 2>&1

if errorlevel 1 (
    echo.
    echo %RED%ERROR:%RESET% Failed to add registry entries.
    pause
    exit /b 1
)
echo       %GREEN%Done.%RESET%

echo.
echo %BOLD%%CYAN%========================================%RESET%
echo %BOLD%%GREEN%  Registration complete!%RESET%
echo %BOLD%%CYAN%========================================%RESET%
echo.
echo %BOLD%Next steps:%RESET%
echo   1. Start %YELLOW%bin\Speechify.exe%RESET% (the TTS server)
echo   2. Open Balabolka and select %CYAN%"Speechify Tom"%RESET%
echo   3. To switch voices, edit %YELLOW%config\SWIttsConfig.xml%RESET%
echo.
echo %BOLD%NOTE:%RESET% Speechify.exe must be running whenever you
echo       want to use any Speechify voice.
echo.
pause

endlocal
exit /b 0
