; Qronos offline WebView2 prerequisite (docs/offline-windows-installer.md).
;
; tauri.conf.json sets webviewInstallMode to "skip", so Tauri's own WebView2
; section neither downloads nor embeds anything. This hook installs the
; Microsoft Edge WebView2 Runtime from the Evergreen Standalone Installer that
; build_support.build_desktop verified against build_support/
; webview2_prerequisite.json and embedded at compile time. The installer never
; downloads anything. The build downloads nothing either, once the one-time
; developer tool preparation in that document is done; build_desktop refuses
; to start without it.
;
; build_desktop generates a small wrapper that defines the values below and
; includes this file. Built any other way (for example a plain
; `npm run tauri build`), the installer refuses to compile rather than ship
; without the prerequisite or fall back to Microsoft's download bootstrapper.

!ifndef QRONOS_WEBVIEW2_INSTALLER
  !error "Qronos installers must be built with build_support.build_desktop: the verified offline WebView2 installer was not provided."
!endif
!ifndef QRONOS_WEBVIEW2_VERSION
  !error "QRONOS_WEBVIEW2_VERSION is not defined."
!endif
; The Microsoft Visual C++ 2015-2022 Redistributable (x64), verified against build_support/
; vcredist_prerequisite.json and embedded the same way.
!ifndef QRONOS_VCREDIST_INSTALLER
  !error "Qronos installers must be built with build_support.build_desktop: the verified Visual C++ redistributable was not provided."
!endif
!ifndef QRONOS_VCREDIST_VERSION
  !error "QRONOS_VCREDIST_VERSION is not defined."
!endif
!ifndef QRONOS_VCREDIST_MAJOR
  !error "QRONOS_VCREDIST_MAJOR is not defined."
!endif
!ifndef QRONOS_VCREDIST_MINOR
  !error "QRONOS_VCREDIST_MINOR is not defined."
!endif
!ifndef QRONOS_VCREDIST_BUILD
  !error "QRONOS_VCREDIST_BUILD is not defined."
!endif

!include "LogicLib.nsh"
!include "x64.nsh"
!include "${__FILEDIR__}\qronos-install-root.nsh"

; The Qronos template (installer-template/installer.nsi) refuses to compile without these hooks.
!define QRONOS_TEMPLATE_HOOKS

!define QRONOS_WEBVIEW2_APPID "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define QRONOS_WEBVIEW2_FILE "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"

; Puts the installed WebView2 Runtime version in $R9, or "" when it is absent.
; Registry locations and the "0.0.0.0" rule follow Microsoft's detection
; guidance for the Evergreen Runtime (per-machine, then per-user).
!macro QRONOS_WEBVIEW2_VERSION_TO_R9
  StrCpy $R9 ""
  ${If} ${RunningX64}
    ReadRegStr $R9 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${QRONOS_WEBVIEW2_APPID}" "pv"
  ${Else}
    ReadRegStr $R9 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${QRONOS_WEBVIEW2_APPID}" "pv"
  ${EndIf}
  ${If} $R9 == ""
  ${OrIf} $R9 == "0.0.0.0"
    ReadRegStr $R9 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\${QRONOS_WEBVIEW2_APPID}" "pv"
  ${EndIf}
  ${If} $R9 == "0.0.0.0"
    StrCpy $R9 ""
  ${EndIf}
!macroend

; Stops the installation. With the Qronos template (installer-template/) these
; run in NSIS_HOOK_PREINSTALL BEFORE SetOutPath, so no install folder has been
; created yet and nothing under it is changed or removed. A probe harness can
; define QRONOS_PROBE_RESULT to also record the message in a file; the installer
; never defines it.
!macro QRONOS_REFUSE MESSAGE
  !ifdef QRONOS_PROBE_RESULT
    FileOpen $R7 "${QRONOS_PROBE_RESULT}" w
    FileWrite $R7 "${MESSAGE}"
    FileClose $R7
  !endif
  Abort "${MESSAGE}"
!macroend

; After a material install step (the Qronos template inserts it right after each one): NSIS commands that fail
; in a silent install (File, WriteUninstaller, WriteRegStr, CreateShortcut) only set the error flag and
; continue, so the installation stops here. IfErrors also clears the flag for the next step.
!macro QRONOS_STEP STEP
  ${If} ${Errors}
    !ifdef QRONOS_PROBE_RESULT
      Push "${STEP}"
      Call QronosProbeStepFailed
    !endif
    Abort "Qronos was not completely installed: ${STEP} failed. Run the installer again."
  ${EndIf}
!macroend

; Immediately before NSIS_HOOK_POSTINSTALL: the last step check, so every step above passed its QRONOS_STEP.
!macro QRONOS_INSTALL_COMPLETE
  !insertmacro QRONOS_STEP "the last install step"
!macroend

!ifdef QRONOS_PROBE_RESULT
Function QronosProbeStepFailed
  Exch $R6
  FileOpen $R7 "${QRONOS_PROBE_RESULT}" w
  FileWrite $R7 "step failed: $R6"
  FileClose $R7
  Pop $R6
FunctionEnd
!endif

!macro QRONOS_FAIL MESSAGE
  !ifdef QRONOS_PROBE_RESULT
    FileOpen $R7 "${QRONOS_PROBE_RESULT}" w
    FileWrite $R7 "${MESSAGE}"
    FileClose $R7
  !endif
  Abort "${MESSAGE}"
!macroend

; Unpacks the embedded installer and runs it. A process that cannot be started
; (ExecWait sets the error flag and leaves the exit-code variable untouched) is
; reported as such; a non-zero exit code is reported with the code.
!macro QRONOS_WEBVIEW2_RUN_INSTALLER
  InitPluginsDir
  File "/oname=$PLUGINSDIR\${QRONOS_WEBVIEW2_FILE}" "${QRONOS_WEBVIEW2_INSTALLER}"
  StrCpy $R8 ""
  ClearErrors
  ExecWait '"$PLUGINSDIR\${QRONOS_WEBVIEW2_FILE}" /silent /install' $R8
  ${If} ${Errors}
    Delete "$PLUGINSDIR\${QRONOS_WEBVIEW2_FILE}"
    !insertmacro QRONOS_FAIL "The Microsoft Edge WebView2 Runtime installer could not be started. Qronos was not installed."
  ${EndIf}
  Delete "$PLUGINSDIR\${QRONOS_WEBVIEW2_FILE}"
  ${If} $R8 == ""
  ${OrIf} $R8 <> 0
    !insertmacro QRONOS_FAIL "The Microsoft Edge WebView2 Runtime could not be installed (exit code $R8). Qronos was not installed."
  ${EndIf}
!macroend

!define QRONOS_VCREDIST_FILE "vc_redist.x64.exe"
!define QRONOS_VCREDIST_KEY "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"

; Reads the registered x64 Visual C++ 2015-2022 runtime from one registry view into $R9 ("1" installed),
; $R6 (major), $R5 (minor) and $R4 (build); values a view does not have read as "".
!macro QRONOS_VCREDIST_READ_VIEW VIEW
  SetRegView ${VIEW}
  ReadRegDWORD $R9 HKLM "${QRONOS_VCREDIST_KEY}" "Installed"
  ReadRegDWORD $R6 HKLM "${QRONOS_VCREDIST_KEY}" "Major"
  ReadRegDWORD $R5 HKLM "${QRONOS_VCREDIST_KEY}" "Minor"
  ReadRegDWORD $R4 HKLM "${QRONOS_VCREDIST_KEY}" "Bld"
!macroend

; Puts "1" in $R9 when the x64 Visual C++ 2015-2022 runtime is registered at the pinned version or newer,
; else "". Registry location and values follow Microsoft's detection guidance (64-bit view, then 32-bit).
!macro QRONOS_VCREDIST_SATISFIED_TO_R9
  !insertmacro QRONOS_VCREDIST_READ_VIEW 64
  ${If} $R9 != "1"
    !insertmacro QRONOS_VCREDIST_READ_VIEW 32
  ${EndIf}
  SetRegView lastused
  ${If} $R9 == "1"
    StrCpy $R9 ""
    ${If} $R6 > ${QRONOS_VCREDIST_MAJOR}
      StrCpy $R9 "1"
    ${ElseIf} $R6 = ${QRONOS_VCREDIST_MAJOR}
      ${If} $R5 > ${QRONOS_VCREDIST_MINOR}
        StrCpy $R9 "1"
      ${ElseIf} $R5 = ${QRONOS_VCREDIST_MINOR}
      ${AndIf} $R4 >= ${QRONOS_VCREDIST_BUILD}
        StrCpy $R9 "1"
      ${EndIf}
    ${EndIf}
  ${Else}
    StrCpy $R9 ""
  ${EndIf}
!macroend

; Unpacks the embedded redistributable and runs it (it asks Windows for administrator consent itself when it
; must install). Accepted exit codes: 0 (installed), 1638 (a newer version is already installed) and 3010
; (installed, a restart completes it); anything else, or a process that cannot be started, stops here.
!macro QRONOS_VCREDIST_RUN_INSTALLER
  InitPluginsDir
  File "/oname=$PLUGINSDIR\${QRONOS_VCREDIST_FILE}" "${QRONOS_VCREDIST_INSTALLER}"
  StrCpy $R8 ""
  ClearErrors
  ExecWait '"$PLUGINSDIR\${QRONOS_VCREDIST_FILE}" /install /quiet /norestart' $R8
  ${If} ${Errors}
    Delete "$PLUGINSDIR\${QRONOS_VCREDIST_FILE}"
    !insertmacro QRONOS_FAIL "The Microsoft Visual C++ Redistributable installer could not be started. Qronos was not installed."
  ${EndIf}
  Delete "$PLUGINSDIR\${QRONOS_VCREDIST_FILE}"
  ${If} $R8 == ""
    !insertmacro QRONOS_FAIL "The Microsoft Visual C++ Redistributable could not be installed (no exit code). Qronos was not installed."
  ${EndIf}
  ${If} $R8 <> 0
  ${AndIf} $R8 <> 1638
  ${AndIf} $R8 <> 3010
    !insertmacro QRONOS_FAIL "The Microsoft Visual C++ Redistributable could not be installed (exit code $R8). Qronos was not installed."
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  Push $R7
  Push $R8
  Push $R9
  ; First: the install folder must be Qronos's own before anything is checked, killed, copied or removed.
  Call QronosCheckInstallRoot
  ${If} $QronosMessage != ""
    !insertmacro QRONOS_REFUSE "$QronosMessage"
  ${EndIf}
  ; The previous installation's whole file list is checked against the disk (no link, junction or wrong
  ; kind on the way to any entry) before anything is stopped, removed or installed.
  StrCpy $QronosValidateOnly "1"
  Call QronosRemoveOwnedPayload
  StrCpy $QronosValidateOnly ""
  ${If} $QronosMessage != ""
    !insertmacro QRONOS_REFUSE "$QronosMessage"
  ${EndIf}
  ; The prerequisite comes BEFORE anything of the previous installation changes: if it cannot be
  ; completed, the previous version stays exactly as it was.
  !insertmacro QRONOS_WEBVIEW2_VERSION_TO_R9
  ${If} $R9 == ""
    ${IfNot} ${IsNativeAMD64}
      !insertmacro QRONOS_FAIL "Qronos needs the Microsoft Edge WebView2 Runtime. This installer contains only the x64 runtime, which does not match this computer. Nothing was installed."
    ${EndIf}
    DetailPrint "Installing the Microsoft Edge WebView2 Runtime ${QRONOS_WEBVIEW2_VERSION} from this installer (no internet needed)..."
    !insertmacro QRONOS_WEBVIEW2_RUN_INSTALLER
    ; The exit code alone is not trusted: the runtime must now be registered.
    !insertmacro QRONOS_WEBVIEW2_VERSION_TO_R9
    ${If} $R9 == ""
      !insertmacro QRONOS_FAIL "The Microsoft Edge WebView2 Runtime installer finished but the runtime is still not registered. Qronos was not installed."
    ${EndIf}
    DetailPrint "Microsoft Edge WebView2 Runtime $R9 is installed."
  ${Else}
    DetailPrint "Microsoft Edge WebView2 Runtime $R9 is already installed."
  ${EndIf}
  ; The Visual C++ runtime too, still BEFORE anything of the previous installation changes.
  Push $R4
  Push $R5
  Push $R6
  !insertmacro QRONOS_VCREDIST_SATISFIED_TO_R9
  ${If} $R9 == ""
    ${IfNot} ${IsNativeAMD64}
      !insertmacro QRONOS_FAIL "Qronos needs the Microsoft Visual C++ Redistributable (x64), which does not match this computer. Nothing was installed."
    ${EndIf}
    DetailPrint "Installing the Microsoft Visual C++ Redistributable (x64) ${QRONOS_VCREDIST_VERSION} from this installer (no internet needed)..."
    !insertmacro QRONOS_VCREDIST_RUN_INSTALLER
    ${If} $R8 = 3010
      ; Microsoft says a restart completes the runtime: the installer's own reboot state, so the finish page
      ; offers the restart (MUI2 reboot support) instead of presenting Qronos as ready to use at once.
      SetRebootFlag true
      DetailPrint "Windows needs a restart to finish the Microsoft Visual C++ Redistributable."
    ${EndIf}
    ; The exit code alone is not trusted: the pinned runtime (or newer) must now be registered.
    !insertmacro QRONOS_VCREDIST_SATISFIED_TO_R9
    ${If} $R9 == ""
      !insertmacro QRONOS_FAIL "The Microsoft Visual C++ Redistributable installer finished but the runtime is still not registered. Qronos was not installed."
    ${EndIf}
    DetailPrint "Microsoft Visual C++ Redistributable (x64) is installed."
  ${Else}
    DetailPrint "Microsoft Visual C++ Redistributable (x64) ${QRONOS_VCREDIST_VERSION} or newer is already installed."
  ${EndIf}
  Pop $R6
  Pop $R5
  Pop $R4
  ; Replace, never mix: once Qronos is not running, remove exactly the previous installation's listed
  ; files, before anything new is installed.
  !ifmacrodef CheckIfAppIsRunning
    !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
  !endif
  Call QronosRemoveOwnedPayload
  ${If} $QronosMessage != ""
    !insertmacro QRONOS_REFUSE "$QronosMessage"
  ${EndIf}
  Pop $R9
  Pop $R8
  Pop $R7
!macroend

; First in the Uninstall section, before anything is stopped, removed or unregistered: the install folder
; must be Qronos's own (protected roots and ancestors, no link; a folder with content must carry a valid
; Qronos file list), the whole file list is checked against the disk, the app is stopped, and exactly the
; listed files and folders are removed. The Qronos template has
; no other file deletion in its Uninstall section (except uninstall.exe by exact name).
!macro NSIS_HOOK_PREUNINSTALL
  Call un.QronosCheckInstallRoot
  ${If} $QronosMessage != ""
    !insertmacro QRONOS_REFUSE "$QronosMessage Nothing was removed."
  ${EndIf}
  ; Without its own file list nothing here is provably Qronos's: refuse before anything is touched.
  ${IfNot} ${FileExists} "$INSTDIR\${QRONOS_MANIFEST}"
    !insertmacro QRONOS_REFUSE "This folder has no Qronos file list, so nothing in it is removed. Nothing was removed."
  ${EndIf}
  StrCpy $QronosValidateOnly "1"
  Call un.QronosRemoveOwnedPayload
  StrCpy $QronosValidateOnly ""
  ${If} $QronosMessage != ""
    !insertmacro QRONOS_REFUSE "$QronosMessage"
  ${EndIf}
  !ifmacrodef CheckIfAppIsRunning
    !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
  !endif
  Call un.QronosRemoveOwnedPayload
  ${If} $QronosMessage != ""
    !insertmacro QRONOS_REFUSE "$QronosMessage"
  ${EndIf}
!macroend

; Last in the Uninstall section: the install folder, only when it is empty. Unlisted files stay.
!macro NSIS_HOOK_POSTUNINSTALL
  RMDir "$INSTDIR"
!macroend
