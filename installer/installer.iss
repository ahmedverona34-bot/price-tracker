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
;  * Wizard is Arabic (ArabicLanguage.isl) with RTL mirroring.
;  * No "run after install" checkbox: a price tracker that auto-launches
;    feels like an installer, not an app.
;  * Uninstaller is registered, and uninstall keeps the user's settings.
;
; Build:  ISCC.exe installer.iss   (Inno Setup 6)

#define AppName "متتبع الأسعار"
#define AppNameEn "Price Tracker"
#define AppVersion "1.0.8"
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
; Arabic UI
LanguageDetectionMethod=uilanguage

[Languages]
; Inno Setup ships an Arabic translation. Using it makes the whole wizard
; Arabic, which is the point: the audience never reads English.
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"

[CustomMessages]
arabic.WelcomeLabel1= متتبع الأسعار
arabic.WelcomeLabel2= يبحث عن سعر الهاتف في كل المتاجر مرة واحدة، ويصدّر Excel.

[Tasks]
Name: "desktopicon"; Description: "إنشاء اختصار على سطح المكتب"; GroupDescription: "اختصارات:"
Name: "quicklaunchicon"; Description: "تثبيت في شريط الأدوات السريع"; GroupDescription: "اختصارات:"; Flags: unchecked

[Files]
; Program files from the PyInstaller onedir build.
Source: "..\dist\PriceTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\إزالة {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "تشغيل {#AppName}"; Flags: nowait postinstall skipifsilent


