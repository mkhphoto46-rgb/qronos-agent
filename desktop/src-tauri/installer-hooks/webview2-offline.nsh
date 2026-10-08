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

!include "LogicLib.nsh"
!include "x64.nsh"
!include "${__FILEDIR__}\qronos-install-root.nsh"

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

; Stops the installation. SetOutPath $INSTDIR has already created the install
; folder, so it is removed again, but only if it is still empty (RMDir without
; /r never deletes files). A probe harness can define QRONOS_PROBE_RESULT to
; also record the message in a file; the installer never defines it.
; Refuses before anything was installed into a folder that is not Qronos's own;
; nothing is removed.
!macro QRONOS_REFUSE MESSAGE
  !ifdef QRONOS_PROBE_RESULT
    FileOpen $R7 "${QRONOS_PROBE_RESULT}" w
    FileWrite $R7 "${MESSAGE}"
    FileClose $R7
  !endif
  Abort "${MESSAGE}"
!macroend

!macro QRONOS_FAIL MESSAGE
  !ifdef QRONOS_PROBE_RESULT
    FileOpen $R7 "${QRONOS_PROBE_RESULT}" w
    FileWrite $R7 "${MESSAGE}"
    FileClose $R7
  !endif
  SetOutPath "$TEMP"  ; $INSTDIR is the current directory, which Windows will not remove
  RMDir "$INSTDIR"
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

!macro NSIS_HOOK_PREINSTALL
  Push $R7
  Push $R8
  Push $R9
  ; The install folder must be Qronos's own before anything is installed.
  Call QronosCheckInstallRoot
  ${If} $QronosMessage != ""
    !insertmacro QRONOS_REFUSE "$QronosMessage"
  ${EndIf}
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
  ; Replace, never mix: remove exactly the previous installation's listed files,
  ; once Qronos is not running, before the new files are copied.
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

; After Tauri's own uninstall steps: remove whatever the installation's file list
; still names, and the install folder when it is empty. Unlisted files stay.
!macro NSIS_HOOK_POSTUNINSTALL
  Call un.QronosRemoveOwnedPayload
  ${If} $QronosMessage != ""
    DetailPrint "$QronosMessage"
  ${EndIf}
  RMDir "$INSTDIR"
!macroend
