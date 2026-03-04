@echo off
setlocal
REM This script registers the Speechify Tom voice for use in Balabolka or similar TTS programs.
REM It assumes the voice DLL is located in a "bin" subfolder relative to this script.
REM NOTE: THIS BATCH FILE MUST BE RUN AS ADMINISTRATOR TO MODIFY THE REGISTRY SUCCESSFULLY. IT WILL FAIL SILENTLY OTHERWISE.
REM Set the path to the Speechify Tom voice DLL, we should get this from the user or base it off `(this folder)/bin`
echo Starting registration of Speechify Tom voice... If you see ANY errors, please ensure you are running this batch script as an Administrator. (Right click -> Run as Administrator).
set "VOICE_DLL=%~dp0bin\SAPI5Speechify.dll"
REM Check if the DLL file exists
if not exist "%VOICE_DLL%" (
    echo Error: Voice DLL not found at %VOICE_DLL%. ABORTING.
    pause
    exit /b 1
)
REM Register the voice DLL using regsvr32
echo Registering Speechify Tom voice...
regsvr32 "%VOICE_DLL%"
if errorlevel 1 (
    echo Error: Failed to register the voice DLL. ABORTING.
    pause
    exit /b 1
)
echo Speechify Tom voice registered successfully. Adding values to registry...
REM Add necessary registry entries for Balabolka or similar programs, the exact keys should look like this:
REM [HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom]
REM @="Speechify Tom"
REM "409"="Speechify Tom"
REM "CLSID"="{0215FC19-483E-4AF7-B608-1FF364DCCB56}"
REM "VoicePath"="C:\\Users\\Wags\\Documents\\Speechify\\bin\\SAPI5Speechify.dll"
REM "EnginePath"="C:\\Users\\Wags\\Documents\\Speechify\\bin\\SAPI5Speechify.dll"
REM [HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom\Attributes]
REM "Language"="409"
REM "Name"="Speechify Tom"
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom" /ve /d "Speechify Tom" /f
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom" /v "409" /d "Speechify Tom" /f
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom" /v "CLSID" /d "{0215FC19-483E-4AF7-B608-1FF364DCCB56}" /f
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom" /v "VoicePath" /d "%VOICE_DLL%" /f
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom" /v "EnginePath" /d "%VOICE_DLL%" /f
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom\Attributes" /v "Language" /d "409" /f
reg add "HKLM\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens\Speechify Tom\Attributes" /v "Name" /d "Speechify Tom" /f
if errorlevel 1 (
    echo Error: Failed to add registry entries. ABORTING.
    pause
    exit /b 1
)
echo Registry entries added successfully.
echo All registration steps completed successfully! You may need to restart Balabolka to see the new voice. Remember to run Speechify.exe in the bin folder to get the synthesis server up and running. Have fun using Speechify Tom!
pause
endlocal
exit /b 0
