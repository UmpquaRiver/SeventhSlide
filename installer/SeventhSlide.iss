; ============================================================================
; SeventhSlide — Windows installer (Inno Setup)
;
; Produces a setup wizard (SeventhSlide-<version>-Setup.exe) that installs the
; Electron app from ..\electron-dist\win-unpacked into Program Files, creates
; Start Menu / optional Desktop shortcuts, and OPTIONALLY copies the user-manual
; PDF onto the user's Desktop (a wizard checkbox).
;
; Build the app folder first (from the project root):
;     pyinstaller lyrics.spec      ; freezes the backend -> dist\lyrics-slideshow.exe
;     npm run build                ; electron-builder "dir" target -> electron-dist\win-unpacked
; then compile this script:
;     "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\SeventhSlide.iss
; Output lands in ..\installer-output\.
;
; Paths below are relative to this .iss file (installer\), so SourceDir is the
; project root one level up.
; ============================================================================

#define MyAppName "SeventhSlide"
#define MyAppVersion "1.1.1"
#define MyAppPublisher "SeventhSlide"
#define MyAppExeName "SeventhSlide.exe"
#define MyManualPdf "SeventhSlide-User-Guide.pdf"

[Setup]
; A stable AppId keeps upgrades/uninstall entries consistent across versions.
AppId={{A04C68BD-C8FD-4D8E-B9BB-9261534F0445}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
; The Electron build is 64-bit; only allow / install on 64-bit Windows.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Installing into Program Files needs admin; allow the user to pick per-user too.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
SetupIconFile=..\icons\seventhslide.ico
DisableProgramGroupPage=yes
OutputDir=..\installer-output
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Optional app desktop shortcut (unchecked by default — Windows convention).
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; The requested option: drop the user-manual PDF on the Desktop. Checked by
; default so it's offered prominently, but the user can untick it.
Name: "desktopmanual"; Description: "Place the user manual (PDF) on my Desktop"; GroupDescription: "User manual:"

[Files]
; The entire Electron app folder (SeventhSlide.exe + resources\, locales\, etc.).
Source: "..\electron-dist\win-unpacked\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Keep a copy of the manual inside the install dir so the Start Menu shortcut
; can always open it, regardless of the Desktop-copy choice.
Source: "..\manual\{#MyManualPdf}"; DestDir: "{app}\manual"; Flags: ignoreversion
; Conditionally copy the manual to the Desktop (only if the task is selected).
Source: "..\manual\{#MyManualPdf}"; DestDir: "{autodesktop}"; Flags: ignoreversion; Tasks: desktopmanual

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} User Guide (PDF)"; Filename: "{app}\manual\{#MyManualPdf}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
