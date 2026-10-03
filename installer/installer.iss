; Inno Setup script for Price Tracker.
;
; Produces PriceTracker-Setup.exe: one file the user downloads, double-clicks,
; and gets a working program with no Python, no runtime, nothing to install
; by hand.
;
; Design decisions that matter for this audience:
;
;  * Default install dir is under LOCALAPPDATA, not Program Files. The app
;    writes its settings, log, cache and prices.xlsx to %APPDATA%\PriceTracker
;    (see data_dir() in price_tracker.py), so it never needs administrator
;    rights and a future in-app updater can replace files without UAC.
;  * Wizard is English and AppName is English, so the Start Menu entry, the
;    taskbar button and Add/Remove Programs all agree. The app's own
;    interface stays Arabic; only the installer is English.
;  * No "run after install" checkbox: a price tracker that auto-launches
;    feels like an installer, not an app.
;  * Uninstaller is registered, and uninstall keeps the user's settings.
;
; Build:  ISCC.exe installer.iss   (Inno Setup 6)
;
; Save this file as UTF-8 WITH a BOM. [InstallDelete] below carries the old
; Arabic shortcut names, and Inno Setup only treats a BOM-less .iss as UTF-8
; from 6.3 onward; with a BOM the Arabic reads correctly on any Inno 6.x.

#define AppName "Price Tracker"
#define AppVersion "1.2.13"
#define AppExeName "PriceTracker.exe"
#define AppPublisher "Price Tracker"
; Wizard and Add/Remove Programs icon. Inno reads the .ico directly, so the
; seven sizes inside it are what Explorer renders at each size.
#define AppIcon "..\assets\PriceTracker.ico"

[Setup]
AppId={{8C4E1A2B-6D3F-4A17-9B5E-2F7A9C1D3E45}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\PriceTracker
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist-installer
OutputBaseFilename=PriceTracker-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
; The app never needs admin, so never ask. Keeps a silent update possible later.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupIconFile={#AppIcon}
UninstallDisplayIcon={#AppIcon}
UninstallDisplayName={#AppName}
; Tell Windows this is a per-user install, not a system one.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
; English UI. There is deliberately no [Languages] section: Inno Setup ships an
; .isl for every language except English, whose messages are built into the
; compiler. Omitting the section is what makes the wizard English, and with no
; languages to choose from there is no language dialog and no fallback.
LanguageDetectionMethod=uilanguage

[CustomMessages]
; Unprefixed, so these apply to the single (English) language. Prefixing with
; a language name would fail: with no [Languages] section the language Inno
; builds is named "default", not "english".
WelcomeLabel1= Price Tracker
WelcomeLabel2= Searches phone prices across every store at once, and exports them to Excel.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "&Shortcuts:"
Name: "quicklaunchicon"; Description: "Add to the &Quick Launch bar"; GroupDescription: "&Shortcuts:"; Flags: unchecked

[Files]
; Program files from the PyInstaller onedir build.
Source: "..\dist\PriceTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; AppName was Arabic up to and including 1.2.10. Anyone updating over an older
; install would otherwise end up with the old Arabic-named shortcuts sitting
; next to the new English ones, because the names differ so nothing is
; overwritten. [InstallDelete] is the first step of installation, so these
; are gone before [Icons] recreates them under the new name.
; {userprograms} because the install is per-user (PrivilegesRequired=lowest).
Type: filesandordirs; Name: "{userprograms}\متتبع الأسعار"
Type: files; Name: "{autodesktop}\متتبع الأسعار.lnk"
Type: files; Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\متتبع الأسعار.lnk"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent


