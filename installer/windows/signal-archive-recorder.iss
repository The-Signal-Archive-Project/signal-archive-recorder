; SPDX-License-Identifier: MPL-2.0
; This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
; If a copy of the MPL was not distributed with this file, You can obtain one at
; https://mozilla.org/MPL/2.0/.
;
; Inno Setup 6 script for the Windows installer. Built by installer/windows/build.py,
; which passes /DAppVersion, /DNumericVersion and /DSourceDir.
;
; - Per-user install, no administrator rights (%LOCALAPPDATA%\Programs).
; - Start-menu entry, optional desktop shortcut, optional start at login.
; - Uninstall keeps recordings, settings and the Hugging Face login by default. It
;   offers "Remove everything" (behind a warning and a second confirmation) and
;   "Remove my Hugging Face token". The removing is done by the app's own
;   `forget` command, which knows where the recordings are.

#define AppName "Signal Archive Recorder"
#define AppExe "SignalArchiveRecorder.exe"
#define CliExe "signal-archive-recorder.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build\windows\dist\SignalArchiveRecorder"
#endif

[Setup]
AppId={{8B0F4C2E-6E7A-4E0B-9C51-3D5A0F6B2C91}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=The Signal Archive Project
AppPublisherURL=https://github.com/The-Signal-Archive-Project
AppSupportURL=https://github.com/The-Signal-Archive-Project/signal-archive-recorder
AppUpdatesURL=https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=SignalArchiveRecorder-{#AppVersion}-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion={#NumericVersion}
VersionInfoProductVersion={#AppVersion}
; The app holds this mutex while running: (un)installing asks to close it first.
AppMutex=SignalArchiveRecorderRunning
CloseApplications=yes

[Tasks]
Name: "autostart"; Description: "Start {#AppName} when I log in (recommended: it only records while WSJT-X runs)"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; The same value the app's own "Start when I log in" checkbox writes, so they agree.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SignalArchiveRecorder"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[Code]
var
  RemoveEverything: Boolean;
  RemoveToken: Boolean;
  EverythingBox: TNewCheckBox;
  TokenBox: TNewCheckBox;
  WarningText: TNewStaticText;

procedure EverythingClicked(Sender: TObject);
begin
  WarningText.Visible := EverythingBox.Checked;
  if EverythingBox.Checked then
  begin
    TokenBox.Checked := True;
    TokenBox.Enabled := False;
  end
  else
    TokenBox.Enabled := True;
end;

function InitializeUninstall(): Boolean;
var
  Form: TSetupForm;
  Intro: TNewStaticText;
  OkButton, CancelButton: TNewButton;
  Margin, Width: Integer;
begin
  Result := True;
  RemoveEverything := False;
  RemoveToken := False;
  if UninstallSilent then
    Exit;  { silent uninstall: keep everything }

  Form := CreateCustomForm(ScaleX(500), ScaleY(300), False, False);
  try
    Form.Caption := 'Uninstall {#AppName}';
    Margin := ScaleX(16);
    Width := Form.ClientWidth - 2 * Margin;

    Intro := TNewStaticText.Create(Form);
    Intro.Parent := Form;
    Intro.AutoSize := False;
    Intro.WordWrap := True;
    Intro.SetBounds(Margin, ScaleY(16), Width, ScaleY(48));
    Intro.Caption := 'The program will be removed. Your recordings, settings and ' +
      'Hugging Face login are kept unless you choose otherwise below, so reinstalling ' +
      'picks up where you left off.';

    EverythingBox := TNewCheckBox.Create(Form);
    EverythingBox.Parent := Form;
    EverythingBox.SetBounds(Margin, ScaleY(76), Width, ScaleY(20));
    EverythingBox.Caption := 'Remove everything: all recordings, settings and logs';
    EverythingBox.OnClick := @EverythingClicked;

    WarningText := TNewStaticText.Create(Form);
    WarningText.Parent := Form;
    WarningText.AutoSize := False;
    WarningText.WordWrap := True;
    WarningText.SetBounds(Margin + ScaleX(18), ScaleY(100), Width - ScaleX(18), ScaleY(64));
    WarningText.Font.Color := clRed;
    WarningText.Font.Style := [fsBold];
    WarningText.Caption := 'Warning: this permanently deletes every recording in your ' +
      'SignalArchive folder, including any that have not been uploaded yet. They cannot ' +
      'be recovered.';
    WarningText.Visible := False;

    TokenBox := TNewCheckBox.Create(Form);
    TokenBox.Parent := Form;
    TokenBox.SetBounds(Margin, ScaleY(172), Width, ScaleY(20));
    TokenBox.Caption := 'Remove my Hugging Face token from this computer';

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'Uninstall';
    OkButton.ModalResult := mrOk;
    OkButton.SetBounds(Form.ClientWidth - Margin - ScaleX(170), Form.ClientHeight - ScaleY(40),
      ScaleX(80), ScaleY(26));

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := 'Cancel';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    CancelButton.SetBounds(Form.ClientWidth - Margin - ScaleX(80), Form.ClientHeight - ScaleY(40),
      ScaleX(80), ScaleY(26));

    Form.ActiveControl := CancelButton;
    if Form.ShowModal() <> mrOk then
    begin
      Result := False;
      Exit;
    end;
    RemoveEverything := EverythingBox.Checked;
    RemoveToken := TokenBox.Checked or RemoveEverything;
  finally
    Form.Free();
  end;

  if RemoveEverything then
  begin
    if MsgBox('Are you sure you want to permanently delete ALL your recordings, ' +
        'including any not uploaded yet, plus your settings and logs?' + #13#10#13#10 +
        'This cannot be undone.', mbCriticalError, MB_YESNO or MB_DEFBUTTON2) <> IDYES then
    begin
      Result := False;  { nothing is uninstalled; run it again to choose differently }
      Exit;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Params: String;
  ResultCode: Integer;
begin
  { Before the program files go: let the app remove what was chosen. It always turns
    off start at login (the program is going away). }
  if CurUninstallStep = usUninstall then
  begin
    Params := 'forget --yes';
    if RemoveEverything then
      Params := Params + ' --everything'
    else if RemoveToken then
      Params := Params + ' --token';
    if not Exec(ExpandConstant('{app}\{#CliExe}'), Params, '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode) then
      Log('forget could not run: ' + SysErrorMessage(ResultCode))
    else
      Log('forget ' + Params + ' exited with ' + IntToStr(ResultCode));
  end;
end;
