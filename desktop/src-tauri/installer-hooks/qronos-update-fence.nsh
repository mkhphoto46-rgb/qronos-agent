; Qronos update fence, hand-over and installation receipt
; (work/opus-production-installer-contract-round10.txt section 3; docs/local-update-packages.md).
;
; NOT YET INCLUDED by webview2-offline.nsh. Tauri 2.9.4's stock template runs
; SetOutPath $INSTDIR and the reinstall page's old-uninstaller ExecWait before
; NSIS_HOOK_PREINSTALL, so wiring waits for the pinned custom template
; (work/opus-production-installer-contract-round10.txt, addendum A). Proven so
; far with compiled probes only (tests/test_update_fence_nsis.py).
;
; What it guarantees, once called first, while this installer or uninstaller runs:
;
; * Fence and exclusive admission first. Before anything is checked, killed,
;   copied, registered or deleted, this process holds the update fence (the
;   named mutex Local\Qronos.UpdateInProgress.v1) and the EXCLUSIVE store
;   admission (a byte-range lock on <data root>\.qronos-store-admission.lock,
;   the same lock core/store_admission.py uses). Both handles are
;   non-inheritable and never closed by this script, so they last exactly as
;   long as this process (normal end, Abort, crash or kill) and nothing it
;   starts can keep them.
; * Update mode ("/S /QRONOSUPDATE", started by core/update_service.py's
;   fenced_launcher). The private record on standard input is read with a bound
;   (1023 bytes, 30 s) and checked exactly; the guard must be this process's
;   real parent; the handed fence handle must be the fence object itself; the
;   receipts folder must be this installer's own <data root>\Updates\receipts;
;   the version must be this installer's own. Anything else refuses with no
;   change. The installer acknowledges with the nonce, waits (60 s) for the
;   guard to let the fence go, takes the exclusive admission (30 s), then waits
;   (60 s) for the guard process to exit, so none of its files are in use
;   when files are replaced. Update mode never falls back to a manual install.
; * Manual install and uninstall: the fence must be free at once and the
;   admission free at once; otherwise Qronos (or another installation) is
;   running and nothing is done.
; * Receipt (update mode, after every install step succeeded): a CREATE_NEW
;   file in the receipts folder binding the nonce, the approved package and
;   manifest SHA-256 and this installer's version. Only the start-up barrier
;   judges it (core/update_service.py _receipt_verdict).
;
; A probe harness may define QRONOS_UPDATE_FENCE_NAME, QRONOS_PROBE_DATA_ROOT
; and QRONOS_PROBE_RESULT (private test values); build_desktop never does.

!include "LogicLib.nsh"
!include "FileFunc.nsh"

!ifndef QRONOS_UPDATE_FENCE_NAME
  !define QRONOS_UPDATE_FENCE_NAME "Local\Qronos.UpdateInProgress.v1"
!endif
!define QRONOS_FENCE_SWITCH "/QRONOSUPDATE"
!define QRONOS_FENCE_RECORD_LIMIT 1023
!define QRONOS_FENCE_ADMISSION_FILE ".qronos-store-admission.lock"
!define /math QRONOS_FENCE_PBI_BYTES ${NSIS_PTR_SIZE} * 6  ; PROCESS_BASIC_INFORMATION

Var QronosFenceUpdate     ; "1" in update mode
Var QronosFenceHandle     ; this process's own handle to the fence (never closed)
Var QronosFenceGiven      ; the guard's duplicated handle (never closed)
Var QronosFenceLock       ; the admission file handle holding the exclusive lock (never closed)
Var QronosFenceRoot       ; the data root this installer derived itself
Var QronosFencePath       ; argument of the reparse check
Var QronosFenceRecord
Var QronosFenceNonce
Var QronosFencePackage
Var QronosFenceManifest
Var QronosFenceReceipts
Var QronosFenceGuardPid
Var QronosFenceGuardTime
Var QronosFenceGuardHandle
Var QronosFenceValue
Var QronosFenceSet
Var QronosFenceOk
Var QronosFenceMessage   ; "" when the fence step passed, otherwise why it refused
Var QronosFenceVersion   ; this installer's own version, set by QRONOS_FENCE_INSTALL ("" refuses update mode)

; Refuse: record the reason for a probe harness, then stop this installer or uninstaller.
!macro QRONOS_FENCE_REFUSE MESSAGE
  !ifdef QRONOS_PROBE_RESULT
    FileOpen $R7 "${QRONOS_PROBE_RESULT}" w
    FileWrite $R7 "refused: ${MESSAGE}"
    FileClose $R7
  !endif
  Abort "${MESSAGE}"
!macroend

!macro QRONOS_FENCE_FUNCTIONS UN

; $QronosFenceValue consists only of characters in $QronosFenceSet (case-sensitive) and is
; 1..$0 characters long -> $QronosFenceOk "1", else "0". Uses $1-$5.
Function ${UN}QronosFenceCharsIn
  StrCpy $QronosFenceOk "0"
  StrLen $1 $QronosFenceValue
  ${If} $1 = 0
  ${OrIf} $1 > $0
    Return
  ${EndIf}
  StrCpy $2 0
  ${Do}
    ${If} $2 >= $1
      ${ExitDo}
    ${EndIf}
    StrCpy $3 $QronosFenceValue 1 $2
    StrCpy $4 0
    ${Do}
      StrCpy $5 $QronosFenceSet 1 $4
      ${If} $5 == ""
        Return  ; not in the set
      ${EndIf}
      StrCmpS $3 $5 0 +2
        ${ExitDo}
      IntOp $4 $4 + 1
    ${Loop}
    IntOp $2 $2 + 1
  ${Loop}
  StrCpy $QronosFenceOk "1"
FunctionEnd

; The data root this installer derives itself (never from the record):
; %QRONOS_DATA_ROOT% when set, else %LOCALAPPDATA%\Qronos, as core/config.py does.
; It must be a plain local "X:\..." path without wildcards or stream names.
Function ${UN}QronosFenceDataRoot
  !ifdef QRONOS_PROBE_DATA_ROOT
    StrCpy $QronosFenceRoot "${QRONOS_PROBE_DATA_ROOT}"
  !else
    ReadEnvStr $QronosFenceRoot "QRONOS_DATA_ROOT"
    ${If} $QronosFenceRoot == ""
      ReadEnvStr $0 "LOCALAPPDATA"
      ${If} $0 == ""
        StrCpy $0 $LOCALAPPDATA
      ${EndIf}
      StrCpy $QronosFenceRoot "$0\Qronos"
    ${EndIf}
  !endif
  StrCpy $0 $QronosFenceRoot 1 -1
  ${If} $0 == "\"
    StrCpy $QronosFenceRoot $QronosFenceRoot -1
  ${EndIf}
  StrCpy $0 $QronosFenceRoot 3
  StrCpy $1 $QronosFenceRoot 1
  StrCpy $QronosFenceValue $1
  StrCpy $QronosFenceSet "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
  Push $0
  StrCpy $0 1
  Call ${UN}QronosFenceCharsIn
  Pop $0
  StrCpy $2 $0 2 1
  StrCpy $QronosFenceMessage ""
  ${If} $QronosFenceOk != "1"
  ${OrIf} $2 != ":\"
    StrCpy $QronosFenceMessage "The Qronos data folder is not a local folder path; nothing was changed."
    Return
  ${EndIf}
  StrLen $1 $QronosFenceRoot
  StrCpy $2 2
  ${Do}
    ${If} $2 >= $1
      ${ExitDo}
    ${EndIf}
    StrCpy $3 $QronosFenceRoot 1 $2
    ${If} $3 == ":"
    ${OrIf} $3 == "*"
    ${OrIf} $3 == "?"
    ${OrIf} $3 == '"'
    ${OrIf} $3 == "<"
    ${OrIf} $3 == ">"
    ${OrIf} $3 == "|"
    ${OrIf} $3 == "/"
      StrCpy $QronosFenceMessage "The Qronos data folder path is not safe to use; nothing was changed."
      Return
    ${EndIf}
    IntOp $2 $2 + 1
  ${Loop}
FunctionEnd

; $QronosFencePath and every folder above it: refused if any existing one is a
; reparse point (junction, symbolic link, mount point).
Function ${UN}QronosFenceNoReparse
  StrCpy $1 $QronosFencePath
  ${Do}
    System::Call 'kernel32::GetFileAttributesW(w r1) i .r2'
    ${If} $2 <> -1
      IntOp $3 $2 & 0x400
      ${If} $3 <> 0
        StrCpy $QronosFenceMessage "$1 is a link or junction; nothing was changed."
        Return
      ${EndIf}
    ${EndIf}
    ; the parent: cut at the last backslash
    StrLen $3 $1
    ${If} $3 <= 3
      ${ExitDo}
    ${EndIf}
    ${Do}
      IntOp $3 $3 - 1
      StrCpy $4 $1 1 $3
      ${If} $4 == "\"
      ${OrIf} $3 <= 2
        ${ExitDo}
      ${EndIf}
    ${Loop}
    ${If} $3 <= 2
      StrCpy $1 $1 3
    ${Else}
      StrCpy $1 $1 $3
    ${EndIf}
  ${Loop}
FunctionEnd

; Open the fence by name (NULL security attributes: not inheritable) -> $QronosFenceHandle;
; WaitForSingleObject(fence, 0) -> $0.
Function ${UN}QronosFenceOpen
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "${QRONOS_UPDATE_FENCE_NAME}") p .r0'
  ${If} $0 = 0
    StrCpy $QronosFenceMessage "Windows could not open the Qronos update fence; nothing was changed."
    StrCpy $0 -1
    Return
  ${EndIf}
  StrCpy $QronosFenceHandle $0
  System::Call 'kernel32::WaitForSingleObject(p $QronosFenceHandle, i 0) i .r0'
FunctionEnd

; The EXCLUSIVE store admission: LockFileEx(EXCLUSIVE|FAIL_IMMEDIATELY) on byte 0, length 1
; of <data root>\.qronos-store-admission.lock, retried without blocking for $9 ms.
; An absent data root holds no writer; it is skipped ($QronosFenceLock stays 0).
Function ${UN}QronosFenceAdmission
  StrCpy $QronosFenceLock 0
  System::Call 'kernel32::GetFileAttributesW(w "$QronosFenceRoot") i .r0'
  ${If} $0 = -1
    Return
  ${EndIf}
  IntOp $1 $0 & 0x10
  ${If} $1 = 0
    StrCpy $QronosFenceMessage "The Qronos data folder is not a folder; nothing was changed."
    Return
  ${EndIf}
  StrCpy $QronosFencePath "$QronosFenceRoot\${QRONOS_FENCE_ADMISSION_FILE}"
  Call ${UN}QronosFenceNoReparse
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  ; GENERIC_READ|GENERIC_WRITE, share read|write (as the writers open it), NULL attributes
  ; (not inheritable), OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL|FILE_FLAG_OPEN_REPARSE_POINT.
  System::Call 'kernel32::CreateFileW(w "$QronosFenceRoot\${QRONOS_FENCE_ADMISSION_FILE}", i 0xC0000000, i 3, p 0, i 4, i 0x200080, p 0) p .r0'
  ${If} $0 = -1
  ${OrIf} $0 = 0
    StrCpy $QronosFenceMessage "The Qronos store admission file cannot be opened; nothing was changed."
    Return
  ${EndIf}
  System::Call '*(i,i,i,i,i,i,i,i,i,i,i,i,i) p .r1'  ; BY_HANDLE_FILE_INFORMATION
  System::Call 'kernel32::GetFileInformationByHandle(p r0, p r1) i .r2'
  System::Call '*$1(i .r3, i, i, i, i, i, i, i, i, i, i .r4)'
  System::Free $1
  IntOp $3 $3 & 0x410
  ${If} $2 = 0
  ${OrIf} $3 <> 0
  ${OrIf} $4 <> 1
    System::Call 'kernel32::CloseHandle(p r0)'
    StrCpy $QronosFenceMessage "The Qronos store admission file is not this profile's own file; nothing was changed."
    Return
  ${EndIf}
  System::Call '*(p 0, p 0, i 0, i 0, p 0) p .r5'  ; OVERLAPPED, offset 0
  StrCpy $6 0
  ${Do}
    System::Call 'kernel32::LockFileEx(p r0, i 3, i 0, i 1, i 0, p r5) i .r7'
    ${If} $7 <> 0
      ${ExitDo}
    ${EndIf}
    ${If} $6 >= $9
      ${ExitDo}
    ${EndIf}
    Sleep 50
    IntOp $6 $6 + 50
  ${Loop}
  System::Free $5
  ${If} $7 = 0
    System::Call 'kernel32::CloseHandle(p r0)'
    StrCpy $QronosFenceMessage "Qronos is still running with its data open; close it first. Nothing was changed."
    Return
  ${EndIf}
  StrCpy $QronosFenceLock $0  ; held until this process ends
FunctionEnd

; Manual install or uninstall: the fence free at once, then the admission free at once.
Function ${UN}QronosFenceManual
  StrCpy $QronosFenceMessage ""
  Call ${UN}QronosFenceDataRoot
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  StrCpy $QronosFencePath $QronosFenceRoot
  Call ${UN}QronosFenceNoReparse
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  Call ${UN}QronosFenceOpen
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  ${If} $0 <> 0
  ${AndIf} $0 <> 0x80
    StrCpy $QronosFenceMessage "Another Qronos update or installation is running; nothing was changed."
    Return
  ${EndIf}
  StrCpy $9 0
  Call ${UN}QronosFenceAdmission
FunctionEnd

!macroend

!insertmacro QRONOS_FENCE_FUNCTIONS ""
!insertmacro QRONOS_FENCE_FUNCTIONS "un."

; ------------------------------------------------------------------ update mode (installer only)

; One "key=value" line ($3) for line number $2 -> the field variables, or $QronosFenceMessage.
Function QronosFenceField
  ${If} $2 = 0
    StrCmpS $3 "QRONOS-UPDATE-FENCE 1" +2 0
      StrCpy $QronosFenceMessage "The update hand-over record is not understood; nothing was changed."
    Return
  ${EndIf}
  ${If} $2 = 1
    StrCpy $5 "nonce="
  ${ElseIf} $2 = 2
    StrCpy $5 "fence="
  ${ElseIf} $2 = 3
    StrCpy $5 "receipts="
  ${ElseIf} $2 = 4
    StrCpy $5 "version="
  ${ElseIf} $2 = 5
    StrCpy $5 "package="
  ${ElseIf} $2 = 6
    StrCpy $5 "manifest="
  ${ElseIf} $2 = 7
    StrCpy $5 "guard="
  ${Else}
    StrCpy $QronosFenceMessage "The update hand-over record has extra lines; nothing was changed."
    Return
  ${EndIf}
  StrLen $6 $5
  StrCpy $7 $3 $6
  StrCmpS $7 $5 +3 0
    StrCpy $QronosFenceMessage "The update hand-over record is not in the expected order; nothing was changed."
    Return
  StrCpy $QronosFenceValue $3 "" $6
  Push $0
  Push $1
  Push $2
  ${If} $2 = 1
  ${OrIf} $2 = 5
  ${OrIf} $2 = 6
    StrCpy $QronosFenceSet "0123456789abcdef"
    StrCpy $0 64
    Call QronosFenceCharsIn
    StrLen $1 $QronosFenceValue
    ${If} $1 <> 64
      StrCpy $QronosFenceOk "0"
    ${EndIf}
  ${ElseIf} $2 = 2
    StrCpy $QronosFenceSet "0123456789"
    StrCpy $0 10
    Call QronosFenceCharsIn
  ${ElseIf} $2 = 3
    StrCpy $QronosFenceOk "1"
  ${ElseIf} $2 = 4
    StrCpy $QronosFenceOk "0"
    ${If} $QronosFenceVersion != ""
      StrCmpS $QronosFenceValue $QronosFenceVersion 0 +2
        StrCpy $QronosFenceOk "1"
    ${EndIf}
  ${Else}
    StrCpy $QronosFenceSet "0123456789:"
    StrCpy $0 32
    Call QronosFenceCharsIn
  ${EndIf}
  Pop $2
  Pop $1
  Pop $0
  ${If} $QronosFenceOk != "1"
    StrCpy $QronosFenceMessage "The update hand-over record has an invalid value; nothing was changed."
    Return
  ${EndIf}
  ${If} $2 = 1
    StrCpy $QronosFenceNonce $QronosFenceValue
  ${ElseIf} $2 = 2
    StrCpy $QronosFenceGiven $QronosFenceValue
  ${ElseIf} $2 = 3
    StrCpy $QronosFenceReceipts $QronosFenceValue
  ${ElseIf} $2 = 5
    StrCpy $QronosFencePackage $QronosFenceValue
  ${ElseIf} $2 = 6
    StrCpy $QronosFenceManifest $QronosFenceValue
  ${ElseIf} $2 = 7
    ; pid:creation-time, each 1..20 digits
    StrCpy $5 0
    StrLen $6 $QronosFenceValue
    ${Do}
      ${If} $5 >= $6
        StrCpy $QronosFenceMessage "The update hand-over record has an invalid guard; nothing was changed."
        Return
      ${EndIf}
      StrCpy $7 $QronosFenceValue 1 $5
      ${If} $7 == ":"
        ${ExitDo}
      ${EndIf}
      IntOp $5 $5 + 1
    ${Loop}
    StrCpy $QronosFenceGuardPid $QronosFenceValue $5
    IntOp $5 $5 + 1
    StrCpy $QronosFenceGuardTime $QronosFenceValue "" $5
    ${If} $QronosFenceGuardPid == ""
    ${OrIf} $QronosFenceGuardTime == ""
      StrCpy $QronosFenceMessage "The update hand-over record has an invalid guard; nothing was changed."
      Return
    ${EndIf}
  ${EndIf}
FunctionEnd

; Read the private record from standard input: at most QRONOS_FENCE_RECORD_LIMIT bytes, within
; 30 s, ended by the guard closing its end. Standard input is then closed. -> $QronosFenceRecord
Function QronosFenceReadRecord
  System::Call 'kernel32::GetStdHandle(i -10) p .r0'
  ${If} $0 = 0
  ${OrIf} $0 = -1
    StrCpy $QronosFenceMessage "No update hand-over record was given; nothing was changed."
    Return
  ${EndIf}
  System::Alloc 1032
  Pop $1
  StrCpy $2 0      ; bytes read
  StrCpy $3 0      ; ms waited
  ${Do}
    System::Call 'kernel32::PeekNamedPipe(p r0, p 0, i 0, p 0, *i .r4, p 0) i .r5 ?e'
    Pop $6
    ${If} $5 = 0
      ${If} $6 = 109  ; ERROR_BROKEN_PIPE: the guard closed its end, the record is complete
        ${ExitDo}
      ${EndIf}
      StrCpy $QronosFenceMessage "The update hand-over record could not be read; nothing was changed."
      ${ExitDo}
    ${EndIf}
    ${If} $4 > 0
      IntOp $7 ${QRONOS_FENCE_RECORD_LIMIT} + 1
      IntOp $7 $7 - $2
      ${If} $4 < $7
        StrCpy $7 $4
      ${EndIf}
      IntPtrOp $8 $1 + $2
      System::Call 'kernel32::ReadFile(p r0, p r8, i r7, *i .r9, p 0) i .r5'
      ${If} $5 = 0
        StrCpy $QronosFenceMessage "The update hand-over record could not be read; nothing was changed."
        ${ExitDo}
      ${EndIf}
      IntOp $2 $2 + $9
      ${If} $2 > ${QRONOS_FENCE_RECORD_LIMIT}
        StrCpy $QronosFenceMessage "The update hand-over record is too long; nothing was changed."
        ${ExitDo}
      ${EndIf}
    ${Else}
      ${If} $3 >= 30000
        StrCpy $QronosFenceMessage "The update hand-over record did not arrive in time; nothing was changed."
        ${ExitDo}
      ${EndIf}
      Sleep 50
      IntOp $3 $3 + 50
    ${EndIf}
  ${Loop}
  System::Call 'kernel32::CloseHandle(p r0)'
  System::Call 'kernel32::SetStdHandle(i -10, p 0)'
  ${If} $QronosFenceMessage == ""
  ${AndIf} $2 > 0
    ; UTF-8 -> UTF-16; an embedded NUL or invalid UTF-8 shortens or fails the decode and is refused.
    System::Call 'kernel32::MultiByteToWideChar(i 65001, i 8, p r1, i r2, w .r9, i ${NSIS_MAX_STRLEN}) i .r5'
    StrLen $6 $9
    ${If} $5 = 0
    ${OrIf} $6 <> $5
      StrCpy $QronosFenceMessage "The update hand-over record is not valid text; nothing was changed."
    ${EndIf}
    StrCpy $QronosFenceRecord $9
  ${ElseIf} $QronosFenceMessage == ""
    StrCpy $QronosFenceMessage "The update hand-over record is empty; nothing was changed."
  ${EndIf}
  System::Free $1
FunctionEnd

; Split $QronosFenceRecord into exactly 8 LF-ended lines and check each.
Function QronosFenceParseRecord
  StrLen $0 $QronosFenceRecord
  StrCpy $1 0
  StrCpy $2 0
  StrCpy $3 ""
  ${Do}
    ${If} $1 >= $0
      ${ExitDo}
    ${EndIf}
    StrCpy $4 $QronosFenceRecord 1 $1
    ${If} $4 == "$\n"
      Call QronosFenceField
      ${If} $QronosFenceMessage != ""
        Return
      ${EndIf}
      IntOp $2 $2 + 1
      StrCpy $3 ""
    ${Else}
      StrCpy $3 "$3$4"
    ${EndIf}
    IntOp $1 $1 + 1
  ${Loop}
  ${If} $2 <> 8
  ${OrIf} $3 != ""
    StrCpy $QronosFenceMessage "The update hand-over record is incomplete; nothing was changed."
  ${EndIf}
FunctionEnd

; Update mode: record, identities, ACK, fence, admission, guard exit (contract section 3.2).
Function QronosFenceUpdateMode
  StrCpy $QronosFenceMessage ""
  ${IfNot} ${Silent}
    StrCpy $QronosFenceMessage "An update installation must run silently; nothing was changed."
    Return
  ${EndIf}
  Call QronosFenceReadRecord
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  Call QronosFenceParseRecord
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  ; c) the receipts folder is this installer's own, under the data root it derives itself
  Call QronosFenceDataRoot
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  StrCmp $QronosFenceReceipts "$QronosFenceRoot\Updates\receipts" +3 0
    StrCpy $QronosFenceMessage "The update hand-over names another receipts folder; nothing was changed."
    Return
  StrCpy $QronosFencePath $QronosFenceReceipts
  Call QronosFenceNoReparse
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  ; d) the guard is this process's actual parent (and, while alive, the same process)
  System::Call '*(p, p, p, p, p, p) p .r1'  ; PROCESS_BASIC_INFORMATION
  System::Call 'ntdll::NtQueryInformationProcess(p -1, i 0, p r1, i ${QRONOS_FENCE_PBI_BYTES}, p 0) i .r2'
  System::Call '*$1(p, p, p, p, p, p .r3)'
  System::Free $1
  ${If} $2 <> 0
  ${OrIf} $3 != $QronosFenceGuardPid
    StrCpy $QronosFenceMessage "The update hand-over does not come from this installer's own parent; nothing was changed."
    Return
  ${EndIf}
  ; Only PROVEN non-existence (ERROR_INVALID_PARAMETER, 87) lets a parent that cannot be opened count as gone;
  ; then the fence identity below is the proof (guard crash). Access denied or any other failure means the
  ; parent may be alive with its image loaded: refuse before the acknowledgement and before any change
  ; (Media15 F15-1).
  System::Call 'kernel32::OpenProcess(i 0x101000, i 0, i $QronosFenceGuardPid) p .r0 ?e'  ; SYNCHRONIZE|QUERY_LIMITED
  Pop $1
  StrCpy $QronosFenceGuardHandle $0
  ${If} $0 = 0
  ${AndIf} $1 <> 87
    StrCpy $QronosFenceMessage "The update guard is running but cannot be checked (error $1); nothing was changed."
    Return
  ${EndIf}
  ${If} $0 <> 0
    System::Call 'kernel32::GetProcessTimes(p r0, *l .r4, *l .r5, *l .r6, *l .r7) i .r2'
    ${If} $2 = 0
    ${OrIf} $4 != $QronosFenceGuardTime
      StrCpy $QronosFenceMessage "The update hand-over guard is not this installer's parent process; nothing was changed."
      Return
    ${EndIf}
  ${EndIf}
  ; e) the handed handle is the fence object itself; it must not be inheritable
  Call QronosFenceOpen
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  StrCpy $8 $0
  ; The given value must be ANOTHER handle to the same object: this installer's own fence handle
  ; compares equal to itself, so a record naming that value is refused.
  System::Call 'kernelbase::CompareObjectHandles(p $QronosFenceHandle, p $QronosFenceGiven) i .r2'
  ${If} $2 = 0
  ${OrIf} $QronosFenceGiven = $QronosFenceHandle
    StrCpy $QronosFenceMessage "The update hand-over did not give the update fence; nothing was changed."
    Return
  ${EndIf}
  System::Call 'kernel32::SetHandleInformation(p $QronosFenceGiven, i 1, i 0) i .r2'  ; HANDLE_FLAG_INHERIT off
  ; f) acknowledge with the nonce, close standard output, then take the fence over
  System::Call 'kernel32::GetStdHandle(i -11) p .r0'
  ${If} $0 <> 0
  ${AndIf} $0 <> -1
    StrCpy $1 "QRONOS-UPDATE-ACK 1 $QronosFenceNonce$\n"
    StrLen $2 $1
    System::Call 'kernel32::WriteFile(p r0, m r1, i r2, *i .r3, p 0) i .r4'  ; a gone guard: ignored (G4)
    System::Call 'kernel32::CloseHandle(p r0)'
    System::Call 'kernel32::SetStdHandle(i -11, p 0)'
  ${EndIf}
  ${If} $8 <> 0
  ${AndIf} $8 <> 0x80
    ${If} $8 <> 0x102  ; WAIT_TIMEOUT: the guard owns it; anything else is a failure
      StrCpy $QronosFenceMessage "Windows could not take the update fence; nothing was changed."
      Return
    ${EndIf}
    System::Call 'kernel32::WaitForSingleObject(p $QronosFenceHandle, i 60000) i .r8'
    ${If} $8 <> 0
    ${AndIf} $8 <> 0x80
      StrCpy $QronosFenceMessage "The update guard did not hand the fence over in time; nothing was changed."
      Return
    ${EndIf}
  ${EndIf}
  ; g) the exclusive store admission (the guard released its own; writers are fenced meanwhile)
  StrCpy $9 30000
  Call QronosFenceAdmission
  ${If} $QronosFenceMessage != ""
    Return
  ${EndIf}
  ${If} $QronosFenceLock = 0
    StrCpy $QronosFenceMessage "The Qronos data folder is missing; an update needs it. Nothing was changed."
    Return
  ${EndIf}
  ; h) the guard process has exited before anything is replaced
  ${If} $QronosFenceGuardHandle <> 0
    System::Call 'kernel32::WaitForSingleObject(p $QronosFenceGuardHandle, i 60000) i .r2'
    System::Call 'kernel32::CloseHandle(p $QronosFenceGuardHandle)'
    ${If} $2 <> 0
      StrCpy $QronosFenceMessage "The update guard is still running; nothing was changed."
      Return
    ${EndIf}
  ${EndIf}
FunctionEnd

; NSIS_HOOK_PREINSTALL, first. A macro, not a Function, for the version: Tauri's template includes the
; hooks BEFORE it defines VERSION, so only text inserted into a section sees the compiled version.
!macro QRONOS_FENCE_INSTALL
  !ifdef VERSION
    StrCpy $QronosFenceVersion "${VERSION}"
  !else
    StrCpy $QronosFenceVersion ""
  !endif
  Call QronosFenceInstall
!macroend

; Update mode or manual mode, decided by the closed switch only.
Function QronosFenceInstall
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  Push $8
  Push $9
  StrCpy $QronosFenceMessage ""
  StrCpy $QronosFenceUpdate "0"
  ClearErrors
  ${GetOptions} $CMDLINE "${QRONOS_FENCE_SWITCH}" $0
  ${IfNot} ${Errors}
    StrCpy $QronosFenceUpdate "1"
    Call QronosFenceUpdateMode
  ${Else}
    Call QronosFenceManual
  ${EndIf}
  Pop $9
  Pop $8
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; NSIS_HOOK_PREUNINSTALL, first: the uninstaller never accepts the update switch.
Function un.QronosFenceUninstall
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  Push $8
  Push $9
  StrCpy $QronosFenceMessage ""
  ClearErrors
  ${GetOptions} $CMDLINE "${QRONOS_FENCE_SWITCH}" $0
  ${IfNot} ${Errors}
    StrCpy $QronosFenceMessage "The uninstaller does not take an update hand-over; nothing was changed."
  ${Else}
    Call un.QronosFenceManual
  ${EndIf}
  Pop $9
  Pop $8
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; NSIS_HOOK_POSTINSTALL (update mode only): the installation receipt, CREATE_NEW, digits-only name.
Function QronosWriteReceipt
  ${If} $QronosFenceUpdate != "1"
    Return
  ${EndIf}
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  StrCpy $QronosFenceMessage ""
  System::Call 'kernel32::GetFileAttributesW(w "$QronosFenceReceipts") i .r0'
  IntOp $1 $0 & 0x410
  ${If} $0 = -1
  ${OrIf} $1 <> 0x10
    StrCpy $QronosFenceMessage "The receipts folder is missing or a link; no installation receipt was written."
    Goto done
  ${EndIf}
  System::Call 'kernel32::GetCurrentProcessId() i .r1'
  System::Call 'kernel32::GetTickCount64() l .r2'
  StrCpy $3 "$QronosFenceReceipts\$1-$2.receipt"
  ; GENERIC_WRITE|DELETE, no sharing, NULL attributes, CREATE_NEW, NORMAL|OPEN_REPARSE_POINT
  System::Call 'kernel32::CreateFileW(w r3, i 0x40010000, i 0, p 0, i 1, i 0x200080, p 0) p .r0'
  ${If} $0 = -1
  ${OrIf} $0 = 0
    StrCpy $QronosFenceMessage "The installation receipt could not be created."
    Goto done
  ${EndIf}
  StrCpy $1 "QRONOS-INSTALL-RECEIPT 1$\r$\nnonce=$QronosFenceNonce$\r$\npackage=$QronosFencePackage$\r$\nmanifest=$QronosFenceManifest$\r$\nversion=$QronosFenceVersion$\r$\nresult=installed$\r$\n"
  StrLen $2 $1
  System::Call 'kernel32::WriteFile(p r0, m r1, i r2, *i .r4, p 0) i .r1'
  ${If} $1 <> 0
    System::Call 'kernel32::FlushFileBuffers(p r0) i .r1'
  ${EndIf}
  ${If} $1 = 0
  ${OrIf} $4 <> $2
    ; delete exactly this file, through its handle: no partial receipt is left
    System::Call '*(&i1 1) p .r4'
    System::Call 'kernel32::SetFileInformationByHandle(p r0, i 4, p r4, i 1) i .r1'
    System::Free $4
    StrCpy $QronosFenceMessage "The installation receipt could not be written."
  ${EndIf}
  System::Call 'kernel32::CloseHandle(p r0)'
  done:
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd
