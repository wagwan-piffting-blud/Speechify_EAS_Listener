; spfy_setup.iss — Inno Setup script for Speechify (spfy)
;
; Builds a Windows installer that:
;   1. Drops binaries (spfy_sapi.dll, spfy_sapi64.dll, spfy_synth.exe) into
;      Program Files\Speechify
;   2. Drops shared FE data (hpclass + fe_tables + symbol table) into
;      %USERPROFILE%\Documents\Speechify\spfy\ — that's the layout
;      spfy_sapi.dll's path resolution expects (see get_project_root in
;      spfy/src/sapi/spfy_sapi.c).
;   3. Registers the 32-bit SAPI DLL via regsvr32. The DLL's own
;      DllRegisterServer writes both the 32- and 64-bit CLSID hives plus
;      voice tokens for every voice it auto-scans under
;      %USERPROFILE%\Documents\Speechify\en-US\*.
;   4. On uninstall: deregisters cleanly (which removes voice tokens
;      from both registry views) and deletes the bundled binaries.
;
; Voices (VIN/VDB/VCF) are NOT bundled — they're proprietary SpeechWorks
; assets. The user drops them into
; %USERPROFILE%\Documents\Speechify\en-US\<voicename>\ themselves;
; auto-scan picks them up at registration time (re-run regsvr32 after
; adding a voice to refresh the token list).
;
; Build:  iscc spfy_setup.iss
; Override paths: iscc /DBuildDir=C:\tmp\spfy_build32 /DSourceRoot=..  spfy_setup.iss

; ---------------------------------------------------------------------
; Preprocessor — paths and metadata
; ---------------------------------------------------------------------

#ifndef BuildDir
#define BuildDir "C:\tmp\spfy_build32"
#endif

#ifndef SourceRoot
#define SourceRoot ".."
#endif

; Date-based (calver) versioning: SpfyVersion is the user-facing
; YYYY.MM.DD string used in filenames and AppVersion. SpfyVersionInfo
; is the strict X.X.X.X numeric form required by VersionInfoVersion
; (PE VersionInfo resource); CI passes YYYY.MM.DD.<run_number>.
#ifndef SpfyVersion
#define SpfyVersion "0.0.0"
#endif

#ifndef SpfyVersionInfo
#define SpfyVersionInfo "0.0.0.0"
#endif

#define MyAppName       "Speechify (spfy)"
#define MyAppShortName  "spfy"
#define MyAppPublisher  "Speechify Open-Source Reimplementation"
#define MyAppURL        "https://github.com/wagwan-piffting-blud/Speechify_EAS_Listener"
#define MyAppExeName    "spfy_synth.exe"

; ---------------------------------------------------------------------
; [Setup]
; ---------------------------------------------------------------------

[Setup]
; Unique installer identity — NOT the COM CLSID. The COM CLSID is
; {9C3A7D1E-4F5A-4B6C-8EA2-5C71D08F1234}, baked into spfy_sapi.dll.
AppId={{B7EC3D11-1A22-4F2C-9F18-3C7E5E5E3D71}
AppName={#MyAppName}
AppVersion={#SpfyVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#SpfyVersionInfo}
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}

; Installs to "C:\Program Files\Speechify" on 64-bit Windows. The 32-bit
; SAPI DLL still goes here (not Program Files (x86)) because both DLLs
; need to be side-by-side for the 32-bit DLL's DllRegisterServer to
; find spfy_sapi64.dll for the 64-bit-view InprocServer32 entry.
DefaultDirName={autopf}\Speechify
DefaultGroupName=Speechify
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableDirPage=no

; SAPI registration touches HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens
; and HKLM\SOFTWARE\Classes\CLSID — admin only.
PrivilegesRequired=admin

; We intentionally write per-user data ({userdocs}\Speechify\) while
; running elevated. This is supported and works as expected: under
; per-user UAC elevation, SHGetFolderPath(CSIDL_PERSONAL) still
; resolves to the invoking user's Documents (not the admin profile).
; spfy_sapi.dll's get_project_root() uses the same call at runtime,
; so the user-time and install-time paths match. Silence Inno's
; preflight warning about this mix.
UsedUserAreasWarning=no

OutputBaseFilename=spfy-setup-{#SpfyVersion}
OutputDir=dist
Compression=lzma2/ultra
SolidCompression=yes

; 64-bit installer behavior — needed so {syswow64}/regsvr32 resolves
; correctly when registering the 32-bit COM DLL on a 64-bit host.
; "x64compatible" is the modern Inno 6.3+ identifier (covers x64 and arm64
; running x64 binaries). Falls back to "x64" on older Inno.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\spfy.ico
WizardStyle=modern

; Branding — single 256x256 multi-resolution .ico sourced from the
; installer/ directory. Used by:
;   * SetupIconFile         — the icon Explorer shows for spfy-setup-*.exe
;   * UninstallDisplayIcon  — the icon in Settings > Apps & features /
;                             Control Panel > Programs and Features
;   * [Icons] IconFilename  — Start Menu shortcuts that want app branding
;
; Wizard small image — the icon in the top-right corner of every
; wizard page after Welcome. BMP only (not ICO). Multiple comma-
; separated paths let Inno pick the best for the user's DPI:
;   1.00x  55x58
;   1.25x  64x68    (uncomment + add the file if you generate it)
;   1.50x  83x88
;   2.00x 110x116
;   2.50x 138x145
;   3.00x 164x174
; Recommend at least 1x + 2x to cover modern Windows hi-DPI displays.
WizardSmallImageFile=spfy_wizard_small.bmp,spfy_wizard_small_150.bmp,spfy_wizard_small_200.bmp

; Optional: the big banner on the Welcome and Finish pages. 164x314 px
; at 1x; same multi-DPI convention applies.
;   WizardImageFile=spfy_wizard.bmp,spfy_wizard_150.bmp,spfy_wizard_200.bmp

SetupIconFile=spfy.ico

; Show "Speechify (spfy)" in Add/Remove Programs.
AppContact={#MyAppURL}

; ---------------------------------------------------------------------
; [Languages]
; ---------------------------------------------------------------------

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ---------------------------------------------------------------------
; [Files]
; ---------------------------------------------------------------------

[Files]
; --- Binaries → {app} ---
; The 32-bit SAPI DLL gets regsvr32'd via [Run] below. We DON'T use
; Inno's `regserver` flag because:
;   (a) It always uses {sys}\regsvr32 which is 64-bit on x64; our DLL
;       is 32-bit and must register via SysWOW64\regsvr32.
;   (b) The 32-bit DLL's DllRegisterServer writes BOTH the 32- and
;       64-bit CLSID hives + voice tokens — running regsvr32 separately
;       on the 64-bit DLL would double-register.
Source: "{#BuildDir}\src\sapi\spfy_sapi.dll";   DestDir: "{app}"; Flags: ignoreversion 32bit
Source: "{#BuildDir}\src\sapi\spfy_sapi64.dll"; DestDir: "{app}"; Flags: ignoreversion 64bit
Source: "{#BuildDir}\src\cli\spfy_synth.exe";   DestDir: "{app}"; Flags: ignoreversion 32bit

; Helper batch — self-elevating regsvr32 wrapper for re-scanning the
; en-US folder after the user drops new voice directories in. Bundled
; so the Start Menu "Refresh SAPI Voices" shortcut and the post-install
; flow have something to point at.
Source: "refresh_voices.bat"; DestDir: "{app}"; Flags: ignoreversion

; App icon — bundled so UninstallDisplayIcon and Start Menu shortcuts
; can reference {app}\spfy.ico. SetupIconFile (above) reads it at
; compile time, this entry ships it for runtime references.
Source: "spfy.ico"; DestDir: "{app}"; Flags: ignoreversion

; --- Shared FE data → %USERPROFILE%\Documents\Speechify\spfy\ ---
; This layout matches what spfy_sapi.c::get_project_root expects.
; Per-user (not per-machine) so each Windows user gets their own copy;
; matches the en-US\<voice>\ layout that's also per-user.
Source: "{#SourceRoot}\spfy\data\tom_hpclass.bin"; DestDir: "{userdocs}\Speechify\spfy\data"; Flags: ignoreversion
Source: "{#SourceRoot}\spfy\data\fe_tables_a\*";   DestDir: "{userdocs}\Speechify\spfy\data\fe_tables_a"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\spfy\data\fe_tables\*";     DestDir: "{userdocs}\Speechify\spfy\data\fe_tables";   Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\spfy\build\fe_symbol_table.json"; DestDir: "{userdocs}\Speechify\spfy\build"; Flags: ignoreversion

; --- Documentation (best-effort, not all repos will have these) ---
Source: "{#SourceRoot}\spfy\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; ---------------------------------------------------------------------
; [Icons] — Start Menu group
; ---------------------------------------------------------------------

[Icons]
; Primary user-facing action: re-scan the voices folder. The user runs
; this after dropping new voice folders (containing <name>.vin /
; <name>8.vdb / <name>.vcf) into %USERPROFILE%\Documents\Speechify\
; en-US\. The batch self-elevates via UAC and re-runs regsvr32.
Name: "{group}\Refresh SAPI Voices"; Filename: "{app}\refresh_voices.bat"; \
  WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 238
Name: "{group}\Open Voices Folder"; Filename: "{userdocs}\Speechify\en-US"; \
  IconFilename: "{sys}\shell32.dll"; IconIndex: 4
Name: "{group}\Documentation"; Filename: "{app}\README.md"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

; ---------------------------------------------------------------------
; [Run] — post-install actions
; ---------------------------------------------------------------------

[Run]
; Register the 32-bit SAPI DLL. SysWOW64\regsvr32.exe is the 32-bit one
; (yes, the naming is backwards — Windows historical legacy). The DLL's
; DllRegisterServer writes both 32- and 64-bit registry views and
; auto-scans %USERPROFILE%\Documents\Speechify\en-US\ for voices.
Filename: "{syswow64}\regsvr32.exe"; \
  Parameters: "/s ""{app}\spfy_sapi.dll"""; \
  StatusMsg: "Registering SAPI voice DLL..."; \
  Flags: runascurrentuser waituntilterminated

; ---------------------------------------------------------------------
; [UninstallRun] — pre-uninstall actions
; ---------------------------------------------------------------------

[UninstallRun]
; Deregister BEFORE files are deleted (otherwise regsvr32 /u can't find
; the DLL to call DllUnregisterServer). The unregister sweep removes:
;   - HKLM\SOFTWARE\Classes\CLSID\{9C3A7D1E-...}  (both views)
;   - All Speechify_* tokens under
;     HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens (both views)
;   - Same under HKCU
Filename: "{syswow64}\regsvr32.exe"; \
  Parameters: "/u /s ""{app}\spfy_sapi.dll"""; \
  StatusMsg: "Deregistering SAPI voice DLL..."; \
  Flags: runascurrentuser waituntilterminated; \
  RunOnceId: "DeregisterSpfySapi"

; ---------------------------------------------------------------------
; [Code] — install-time sanity checks
; ---------------------------------------------------------------------

[Code]
function InitializeSetup(): Boolean;
var
  WinVersion: TWindowsVersion;
begin
  GetWindowsVersionEx(WinVersion);
  if not WinVersion.NTPlatform or (WinVersion.Major < 6) then
  begin
    MsgBox('This installer requires Windows Vista or newer.',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;
  Result := True;
end;

function CountVoiceDirs(const Root: String): Integer;
var
  Rec: TFindRec;
  Voice: String;
begin
  Result := 0;
  if not DirExists(Root) then Exit;
  if FindFirst(Root + '\*', Rec) then
  try
    repeat
      if (Rec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        if (Rec.Name <> '.') and (Rec.Name <> '..') then
        begin
          Voice := Root + '\' + Rec.Name + '\' + Rec.Name;
          { Count as a voice only if the canonical trio is present. }
          if FileExists(Voice + '.vin')
             and FileExists(Voice + '8.vdb')
             and FileExists(Voice + '.vcf') then
            Result := Result + 1;
        end;
    until not FindNext(Rec);
  finally
    FindClose(Rec);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  VoicesDir: String;
  Found: Integer;
  Msg: String;
  Opened: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    VoicesDir := ExpandConstant('{userdocs}\Speechify\en-US');
    ForceDirectories(VoicesDir);
    Found := CountVoiceDirs(VoicesDir);

    if Found = 0 then
    begin
      { Fresh install — no voices on disk yet. Explain the workflow
        and offer to open the folder. }
      Msg :=
        'Speechify is installed, but no SAPI voices have been registered yet.' + #13#10 + #13#10 +
        'SAPI voices need the raw SpeechWorks voice data (VIN/VDB/VCF), which is NOT bundled. You can find the voices in the GitHub repo or at https://archive.org/details/SpeechifyTom.' + #13#10 + #13#10 +
        'To finish setup:' + #13#10 +
        '  1. Drop each voice folder into:' + #13#10 +
        '       ' + VoicesDir + #13#10 +
        '     (each folder must contain <name>.vin, <name>8.vdb, <name>.vcf)' + #13#10 + #13#10 +
        '  2. Run Start Menu > Speechify > "Refresh SAPI Voices"' + #13#10 +
        '     (or open an elevated cmd and run "%~dpsapi\refresh_voices.bat")' + #13#10 + #13#10 +
        'Open the voices folder in Explorer now?';
      if MsgBox(Msg, mbConfirmation, MB_YESNO) = IDYES then
      begin
        ShellExec('open', VoicesDir, '', '', SW_SHOWNORMAL, ewNoWait, Opened);
      end;
    end
    else
    begin
      { Voices already present (re-install / upgrade) — registration
        already picked them up. Just confirm count. }
      Msg :=
        'Speechify is installed and ' + IntToStr(Found) +
        ' voice(s) registered with SAPI.' + #13#10 + #13#10 +
        'Restart your SAPI client (Balabolka, Narrator, etc.) to see them.' + #13#10 + #13#10 +
        'If you add more voices later, run Start Menu > Speechify > "Refresh SAPI Voices".';
      MsgBox(Msg, mbInformation, MB_OK);
    end;
  end;
end;
