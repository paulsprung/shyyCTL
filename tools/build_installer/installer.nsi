; shyyctl Windows installer (NSIS).
;
; Per-user install (no admin elevation for the installer itself - the
; app's own subprocesses, OpenRGB and SensorHelper, each pop their own UAC
; prompt individually when they need admin rights, per config.yaml's
; launch_elevated settings; shyyctl.exe itself never needs to be admin).
; Installs into %LocalAppData%\shyyctl, adds a Start Menu shortcut that
; opens the web UI on demand, registers a HKCU Run key so shyyctl.exe
; --background launches silently at every login (keeping fan curves/RGB
; effects/the Panorama display alive even with no UI open - main.py's own
; already-running check means a human opening the Start Menu shortcut
; afterwards just gets a browser tab pointed at that already-running
; instance, not a second copy), and writes standard uninstall registry
; keys so it shows up in "Add or Remove Programs".
;
; Built via (see .github/workflows/release.yml, windows-latest ships NSIS
; preinstalled):
;   makensis /DVERSION=1.0.0 /DDIST_DIR=dist\shyyctl /DVENDOR_DIR=vendor tools\build_installer\installer.nsi

!ifndef VERSION
  !define VERSION "0.0.0-dev"
!endif
!ifndef DIST_DIR
  !define DIST_DIR "..\..\dist\shyyctl"
!endif
!ifndef VENDOR_DIR
  !define VENDOR_DIR "..\..\vendor"
!endif

Name "shyyctl"
OutFile "shyyctl-setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\shyyctl"
InstallDirRegKey HKCU "Software\shyyctl" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

!include "MUI2.nsh"

!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "shyyctl" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; Kill anything already running from a previous install before
  ; overwriting its files - an in-place upgrade over a locked, running
  ; shyyctl.exe would otherwise fail partway through with a confusing
  ; "cannot copy file" error instead of just handling it.
  nsExec::Exec 'taskkill /F /IM shyyctl.exe'
  Pop $0

  ; The PyInstaller --onedir output (shyyctl.exe + its _internal/ folder,
  ; containing web/, config.example.yaml, and every bundled Python/native
  ; dependency - see tools/build_installer/shyyctl.spec).
  File /r "${DIST_DIR}\*.*"

  ; vendor/ (ffmpeg.exe, OpenRGB/, SensorHelper/) is built separately by
  ; tools/setup_vendor.py in the release workflow, not by PyInstaller -
  ; staged into the same install directory here so main.py's ROOT-relative
  ; vendor/ lookups (_launch_vendor_openrgb, _launch_vendor_sensor_helper)
  ; find it in the one place it actually looks.
  SetOutPath "$INSTDIR\vendor"
  File /r "${VENDOR_DIR}\*.*"
  SetOutPath "$INSTDIR"

  WriteRegStr HKCU "Software\shyyctl" "InstallDir" "$INSTDIR"

  ; Silent autostart at login - the behaviour the user actually asked for
  ; ("service keeps animations/display alive when the app is closed"). A
  ; HKCU Run key (not a SYSTEM service) because it runs in the interactive
  ; logon session shyyctl's USB/RGB device access needs - a real Windows
  ; service starting before login would very likely lose that access.
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "shyyctl" \
    '"$INSTDIR\shyyctl.exe" --background'

  CreateDirectory "$SMPROGRAMS\shyyctl"
  CreateShortcut "$SMPROGRAMS\shyyctl\shyyctl.lnk" "$INSTDIR\shyyctl.exe"
  CreateShortcut "$SMPROGRAMS\shyyctl\Uninstall shyyctl.lnk" "$INSTDIR\Uninstall.exe"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl" \
    "DisplayName" "shyyctl"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl" \
    "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl" \
    "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl" \
    "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl" \
    "Publisher" "shyyctl"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl" \
    "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl" \
    "NoRepair" 1

  ; Launch immediately after a fresh install so the user lands straight in
  ; the web UI instead of having to go find the Start Menu shortcut - a
  ; normal (non-background) launch, so it opens the browser per
  ; config.yaml's server.open_browser (on by default).
  Exec '"$INSTDIR\shyyctl.exe"'
SectionEnd

Function un.onInit
  nsExec::Exec 'taskkill /F /IM shyyctl.exe'
  Pop $0
FunctionEnd

Section "Uninstall"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\shyyctl"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "shyyctl"
  DeleteRegKey HKCU "Software\shyyctl"

  Delete "$SMPROGRAMS\shyyctl\shyyctl.lnk"
  Delete "$SMPROGRAMS\shyyctl\Uninstall shyyctl.lnk"
  RMDir "$SMPROGRAMS\shyyctl"

  ; Config/state files (config.yaml, curve.json, layout.json, etc.) and
  ; media_gallery/ live in this same directory (see main.py's ROOT) -
  ; removed along with everything else on uninstall. That's a deliberate,
  ; ordinary "uninstall means gone" choice, not an oversight; there's no
  ; separate export/backup step because nothing here asked for one.
  RMDir /r "$INSTDIR"
SectionEnd
