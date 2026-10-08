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

!macro NSIS_HOOK_PREINSTALL
  Push $R8
  Push $R9
  !insertmacro QRONOS_WEBVIEW2_VERSION_TO_R9
  ${If} $R9 == ""
    ${IfNot} ${IsNativeAMD64}
      Abort "Qronos needs the Microsoft Edge WebView2 Runtime. This installer contains only the x64 runtime, which does not match this computer. Nothing was installed."
    ${EndIf}
    DetailPrint "Installing the Microsoft Edge WebView2 Runtime ${QRONOS_WEBVIEW2_VERSION} from this installer (no internet needed)..."
    InitPluginsDir
    File "/oname=$PLUGINSDIR\${QRONOS_WEBVIEW2_FILE}" "${QRONOS_WEBVIEW2_INSTALLER}"
    ExecWait '"$PLUGINSDIR\${QRONOS_WEBVIEW2_FILE}" /silent /install' $R8
    Delete "$PLUGINSDIR\${QRONOS_WEBVIEW2_FILE}"
    ${If} $R8 <> 0
      Abort "The Microsoft Edge WebView2 Runtime could not be installed (exit code $R8). Qronos was not installed."
    ${EndIf}
    !insertmacro QRONOS_WEBVIEW2_VERSION_TO_R9
    ${If} $R9 == ""
      Abort "The Microsoft Edge WebView2 Runtime installer finished but the runtime is still not registered. Qronos was not installed."
    ${EndIf}
    DetailPrint "Microsoft Edge WebView2 Runtime $R9 is installed."
  ${Else}
    DetailPrint "Microsoft Edge WebView2 Runtime $R9 is already installed."
  ${EndIf}
  Pop $R9
  Pop $R8
!macroend
