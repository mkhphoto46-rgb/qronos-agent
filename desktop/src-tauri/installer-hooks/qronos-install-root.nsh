; Qronos install-root guard and owned-payload removal (docs/offline-windows-installer.md).
;
; Included by webview2-offline.nsh. Two guarantees, both enforced while the
; installer or uninstaller runs:
;
; 1. QronosCheckInstallRoot refuses an install folder that is, sits inside or
;    contains a Qronos user-data root (state, Library, backups, app data,
;    models), a system or profile root, a network/device/alternate-stream path,
;    a junction, or a non-empty folder that is not an existing Qronos
;    installation. The folder is compared in its canonical Windows form, so
;    "...\Qronos." and 8.3 short names cannot slip past.
; 2. QronosRemoveOwnedPayload deletes exactly the files that the previous
;    installation's qronos-install-manifest.txt lists, then removes listed
;    folders only when they are empty. Files the manifest does not list (a
;    user's own files) are never touched. An invalid manifest line, a listed
;    folder that is a junction, or a file that cannot be deleted stops it
;    before or without deleting anything further.
;
; A probe harness may define QRONOS_PROBE_PROFILE, QRONOS_PROBE_LOCALAPPDATA,
; QRONOS_PROBE_APPDATA and QRONOS_PROBE_DOCUMENTS to stand in for the user's
; real folders; the installer never defines them.

!include "LogicLib.nsh"

!define QRONOS_MANIFEST "qronos-install-manifest.txt"
!define QRONOS_MANIFEST_HEADER "QRONOS-INSTALL-MANIFEST 1 com.qronos.agent"

Var QronosPath       ; argument and result of QronosCanonical
Var QronosInstall    ; canonical install folder
Var QronosRoot       ; the root QronosRelation compares with
Var QronosRelation   ; "same", "inside", "contains" or "apart"
Var QronosMessage    ; "" when a check passed, otherwise why it refused
Var QronosLine
Var QronosHandle
Var QronosProfile
Var QronosLocal
Var QronosRoaming
Var QronosDocuments
Var QronosChar
Var QronosLexical    ; QronosCanonical's input after GetFullPathNameW/GetLongPathNameW, before links are resolved
Var QronosSegment

!macro QRONOS_INSTALL_ROOT_FUNCTIONS UN

; $QronosPath (absolute) -> the same path with every link and junction in the part
; that exists resolved by Windows (GetFinalPathNameByHandleW on the deepest existing
; folder); the part that does not exist yet is appended unchanged.
Function ${UN}QronosResolveFinal
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  StrCpy $1 $QronosPath
  StrCpy $2 ""
  ; Only local drive paths ("X:\...") are resolved; anything else is refused by callers.
  StrCpy $3 $1 2 1
  ${If} $3 != ":\"
    Goto qronos_final_done
  ${EndIf}
  ${Do}
    System::Call 'kernel32::GetFileAttributesW(w "$1") i .r3'
    ${If} $3 <> -1
      ${Break}
    ${EndIf}
    StrLen $4 $1
    ${If} $4 <= 3
      Goto qronos_final_done
    ${EndIf}
    ${Do}
      IntOp $4 $4 - 1
      StrCpy $5 $1 1 $4
      ${If} $5 == "\"
      ${OrIf} $4 <= 0
        ${Break}
      ${EndIf}
    ${Loop}
    ${If} $4 <= 0
      Goto qronos_final_done
    ${EndIf}
    StrCpy $5 $1 "" $4
    StrCpy $2 "$5$2"
    StrCpy $1 $1 $4
    ${If} $4 = 2
      StrCpy $1 "$1\"
    ${EndIf}
  ${Loop}
  System::Call 'kernel32::CreateFileW(w "$1", i 0, i 7, p 0, i 3, i 0x02000000, p 0) p .r3'
  ${If} $3 = -1
    Goto qronos_final_done
  ${EndIf}
  System::Call 'kernel32::GetFinalPathNameByHandleW(p r3, w .r4, i ${NSIS_MAX_STRLEN}, i 0) i .r5'
  System::Call 'kernel32::CloseHandle(p r3)'
  ${If} $5 > 0
  ${AndIf} $5 < ${NSIS_MAX_STRLEN}
    StrCpy $5 $4 8
    ${If} $5 == "\\?\UNC\"
      StrCpy $4 $4 "" 8
      StrCpy $4 "\\$4"
    ${Else}
      StrCpy $5 $4 4
      ${If} $5 == "\\?\"
        StrCpy $4 $4 "" 4
      ${EndIf}
    ${EndIf}
    StrCpy $5 $4 1 -1
    ${If} $5 == "\"
    ${AndIf} $2 != ""
      StrCpy $4 $4 -1
    ${EndIf}
    StrCpy $QronosPath "$4$2"
  ${EndIf}
qronos_final_done:
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; $QronosPath -> canonical absolute path (GetFullPathNameW, then GetLongPathNameW
; when it exists, then links and junctions resolved) without a trailing backslash
; except after a drive; "" if Windows cannot resolve it. $QronosLexical keeps the
; form before links were resolved, so a caller can tell that a link was on the way.
Function ${UN}QronosCanonical
  Push $1
  Push $2
  System::Call 'kernel32::GetFullPathNameW(w "$QronosPath", i ${NSIS_MAX_STRLEN}, w .r1, p 0) i .r2'
  ${If} $2 = 0
  ${OrIf} $2 >= ${NSIS_MAX_STRLEN}
    StrCpy $QronosPath ""
  ${Else}
    StrCpy $QronosPath $1
    System::Call 'kernel32::GetLongPathNameW(w "$QronosPath", w .r1, i ${NSIS_MAX_STRLEN}) i .r2'
    ${If} $2 > 0
    ${AndIf} $2 < ${NSIS_MAX_STRLEN}
      StrCpy $QronosPath $1
    ${EndIf}
    StrCpy $1 $QronosPath 1 -1
    StrLen $2 $QronosPath
    ${If} $1 == "\"
    ${AndIf} $2 > 3
      StrCpy $QronosPath $QronosPath -1
    ${EndIf}
    StrCpy $QronosLexical $QronosPath
    Call ${UN}QronosResolveFinal
    StrCpy $1 $QronosPath 1 -1
    StrLen $2 $QronosPath
    ${If} $1 == "\"
    ${AndIf} $2 > 3
      StrCpy $QronosPath $QronosPath -1
    ${EndIf}
  ${EndIf}
  Pop $2
  Pop $1
FunctionEnd

; $QronosInstall against $QronosRoot (both canonical) -> $QronosRelation.
; NSIS string comparison ignores case, as Windows does.
Function ${UN}QronosRelation
  Push $1
  Push $2
  Push $3
  StrCpy $QronosRelation "apart"
  ${If} $QronosRoot == ""
    Goto qronos_relation_done
  ${EndIf}
  ${If} $QronosInstall == $QronosRoot
    StrCpy $QronosRelation "same"
    Goto qronos_relation_done
  ${EndIf}
  ; Install folder inside the root?
  StrLen $1 $QronosRoot
  StrCpy $2 $QronosInstall $1
  StrCpy $3 $QronosRoot 1 -1
  ${If} $2 == $QronosRoot
    StrCpy $2 $QronosInstall 1 $1
    ${If} $2 == "\"
    ${OrIf} $3 == "\"
      StrCpy $QronosRelation "inside"
      Goto qronos_relation_done
    ${EndIf}
  ${EndIf}
  ; Install folder containing the root?
  StrLen $1 $QronosInstall
  StrCpy $2 $QronosRoot $1
  StrCpy $3 $QronosInstall 1 -1
  ${If} $2 == $QronosInstall
    StrCpy $2 $QronosRoot 1 $1
    ${If} $2 == "\"
    ${OrIf} $3 == "\"
      StrCpy $QronosRelation "contains"
    ${EndIf}
  ${EndIf}
qronos_relation_done:
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; Refuses when the install folder is exactly $QronosPath (a system or profile root).
Function ${UN}QronosRefuseSame
  ${If} $QronosMessage == ""
  ${AndIf} $QronosPath != ""
    Call ${UN}QronosCanonical
    ${If} $QronosInstall == $QronosPath
      StrCpy $QronosMessage "Qronos cannot be installed directly into $QronosPath. Choose a folder of its own."
    ${EndIf}
  ${EndIf}
FunctionEnd

; Refuses when the install folder is, sits inside or contains user data at $QronosPath.
Function ${UN}QronosRefuseOverlap
  ${If} $QronosMessage == ""
  ${AndIf} $QronosPath != ""
    Call ${UN}QronosCanonical
    StrCpy $QronosRoot $QronosPath
    Call ${UN}QronosRelation
    ${If} $QronosRelation != "apart"
      StrCpy $QronosMessage "The chosen folder overlaps $QronosRoot, where Qronos or your files are kept. Choose a separate folder."
    ${EndIf}
  ${EndIf}
FunctionEnd

Function ${UN}QronosSetRoots
  !ifdef QRONOS_PROBE_PROFILE
    StrCpy $QronosProfile "${QRONOS_PROBE_PROFILE}"
    StrCpy $QronosLocal "${QRONOS_PROBE_LOCALAPPDATA}"
    StrCpy $QronosRoaming "${QRONOS_PROBE_APPDATA}"
    StrCpy $QronosDocuments "${QRONOS_PROBE_DOCUMENTS}"
  !else
    ; The current user's folders, also when installing for all users.
    SetShellVarContext current
    StrCpy $QronosProfile $PROFILE
    StrCpy $QronosLocal $LOCALAPPDATA
    StrCpy $QronosRoaming $APPDATA
    StrCpy $QronosDocuments $DOCUMENTS
    !ifdef INSTALLMODE
      !if "${INSTALLMODE}" == "perMachine"
        SetShellVarContext all
      !endif
    !endif
  !endif
FunctionEnd

; $INSTDIR -> $QronosMessage ("" when the folder may be used).
Function ${UN}QronosCheckInstallRoot
  Push $1
  Push $2
  Push $3
  StrCpy $QronosMessage ""
  Call ${UN}QronosSetRoots
  ; NSIS drops a trailing backslash from $INSTDIR, so a drive root arrives as "X:",
  ; which Windows would resolve to the current folder on that drive. "X:" is the
  ; drive root; any other drive-relative form ("X:folder") is refused.
  StrCpy $QronosPath $INSTDIR
  StrCpy $1 $INSTDIR 1 1
  StrCpy $2 $INSTDIR 1 2
  StrLen $3 $INSTDIR
  ${If} $1 == ":"
    ${If} $3 = 2
      StrCpy $QronosPath "$INSTDIR\"
    ${ElseIf} $2 != "\"
      StrCpy $QronosMessage "Choose a complete folder path, starting with a drive letter and a backslash."
      Goto qronos_check_done
    ${EndIf}
  ${EndIf}
  Call ${UN}QronosCanonical
  StrCpy $QronosInstall $QronosPath
  ${If} $QronosInstall == ""
    StrCpy $QronosMessage "Windows cannot resolve the chosen folder."
    Goto qronos_check_done
  ${EndIf}
  ; A local drive path: "X:\...". Network shares and devices ("\\server", "\\.\CON",
  ; which is what GetFullPathNameW makes of "...\CON") start with two backslashes.
  StrCpy $1 $QronosLexical 2
  StrCpy $2 $QronosLexical 1 1
  StrCpy $3 $QronosLexical 1 2
  ${If} $1 == "\\"
  ${OrIf} $2 != ":"
  ${OrIf} $3 != "\"
    StrCpy $QronosMessage "Choose a folder on a local drive, not a network share or device."
    Goto qronos_check_done
  ${EndIf}
  ; A link or junction anywhere on the way makes the resolved path differ from the
  ; written one. Such a folder is refused whatever it points to, so the checks
  ; below always see the folder that would really be written to.
  ${If} $QronosInstall != $QronosLexical
    StrCpy $QronosMessage "The chosen folder is reached through a link or junction. Choose a folder whose path contains no links."
    Goto qronos_check_done
  ${EndIf}
  ; No ':' after the drive (an alternate data stream).
  StrLen $2 $QronosInstall
  StrCpy $1 2
  ${DoWhile} $1 < $2
    StrCpy $3 $QronosInstall 1 $1
    ${If} $3 == ":"
      StrCpy $QronosMessage "The chosen folder name contains ':'."
      Goto qronos_check_done
    ${EndIf}
    IntOp $1 $1 + 1
  ${Loop}
  ${If} $2 <= 3
    StrCpy $QronosMessage "Qronos cannot be installed directly into a drive's top folder."
    Goto qronos_check_done
  ${EndIf}
  ; The last folder name must not be a device name, also with an extension
  ; ("NUL.txt"). Older Windows maps these to devices; Windows 11 may not, so the
  ; name is checked rather than trusting GetFullPathNameW.
  StrCpy $1 $2
  ${Do}
    IntOp $1 $1 - 1
    StrCpy $3 $QronosInstall 1 $1
  ${LoopUntil} $3 == "\"
  IntOp $1 $1 + 1
  StrCpy $3 $QronosInstall "" $1
  StrCpy $1 0
  StrLen $2 $3
  ${DoWhile} $1 < $2
    StrCpy $QronosChar $3 1 $1
    ${If} $QronosChar == "."
      StrCpy $3 $3 $1
      ${Break}
    ${EndIf}
    IntOp $1 $1 + 1
  ${Loop}
  ${Do}
    StrCpy $QronosChar $3 1 -1
    ${If} $QronosChar != " "
      ${Break}
    ${EndIf}
    StrCpy $3 $3 -1
  ${Loop}
  StrCpy $1 $3 3
  StrCpy $2 $3 1 3
  StrLen $QronosChar $3
  ${If} $3 == "CON"
  ${OrIf} $3 == "PRN"
  ${OrIf} $3 == "AUX"
  ${OrIf} $3 == "NUL"
  ${OrIf} $3 == "CONIN$$"
  ${OrIf} $3 == "CONOUT$$"
    StrCpy $QronosMessage "The chosen folder name is a Windows device name."
    Goto qronos_check_done
  ${EndIf}
  ${If} $QronosChar = 4
    ${If} $1 == "COM"
    ${OrIf} $1 == "LPT"
      ${If} $2 == "1"
      ${OrIf} $2 == "2"
      ${OrIf} $2 == "3"
      ${OrIf} $2 == "4"
      ${OrIf} $2 == "5"
      ${OrIf} $2 == "6"
      ${OrIf} $2 == "7"
      ${OrIf} $2 == "8"
      ${OrIf} $2 == "9"
      ${OrIf} $2 == "${U+00B9}"
      ${OrIf} $2 == "${U+00B2}"
      ${OrIf} $2 == "${U+00B3}"
        StrCpy $QronosMessage "The chosen folder name is a Windows device name."
        Goto qronos_check_done
      ${EndIf}
    ${EndIf}
  ${EndIf}

  ; System and profile roots.
  StrCpy $QronosPath $QronosProfile
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath "$QronosProfile\Documents"
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath $QronosDocuments
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath "$QronosProfile\Desktop"
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath $QronosLocal
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath "$QronosLocal\Programs"
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath $QronosRoaming
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath $WINDIR
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath $PROGRAMFILES
  Call ${UN}QronosRefuseSame
  StrCpy $QronosPath $PROGRAMFILES64
  Call ${UN}QronosRefuseSame

  ; Qronos user data, the Library, backups, app data and models (core/config.py,
  ; core/storage_locations.py, desktop runtime/backend.rs, Tauri's app-data folders).
  StrCpy $QronosPath "$QronosLocal\Qronos"
  Call ${UN}QronosRefuseOverlap
  StrCpy $QronosPath "$QronosProfile\Documents\Qronos"
  Call ${UN}QronosRefuseOverlap
  StrCpy $QronosPath "$QronosProfile\Documents\Qronos Files"
  Call ${UN}QronosRefuseOverlap
  StrCpy $QronosPath "$QronosDocuments\Qronos"
  Call ${UN}QronosRefuseOverlap
  StrCpy $QronosPath "$QronosDocuments\Qronos Files"
  Call ${UN}QronosRefuseOverlap
  StrCpy $QronosPath "$QronosLocal\com.qronos.agent"
  Call ${UN}QronosRefuseOverlap
  StrCpy $QronosPath "$QronosRoaming\com.qronos.agent"
  Call ${UN}QronosRefuseOverlap
  StrCpy $QronosPath "$QronosProfile\.ollama"
  Call ${UN}QronosRefuseOverlap
  !ifndef QRONOS_PROBE_PROFILE
    ReadEnvStr $QronosPath "QRONOS_DATA_ROOT"
    Call ${UN}QronosRefuseOverlap
    ReadEnvStr $QronosPath "OLLAMA_MODELS"
    Call ${UN}QronosRefuseOverlap
  !endif
  ${If} $QronosMessage != ""
    Goto qronos_check_done
  ${EndIf}

  ; The folder itself must not be a junction or symbolic link.
  System::Call 'kernel32::GetFileAttributesW(w "$QronosInstall") i .r1'
  ${If} $1 <> -1
    IntOp $2 $1 & 0x400
    ${If} $2 <> 0
      StrCpy $QronosMessage "The chosen folder is a link to another folder."
      Goto qronos_check_done
    ${EndIf}
  ${EndIf}

  ; A folder with anything in it must be an existing Qronos installation.
  StrCpy $3 ""
  FindFirst $1 $2 "$QronosInstall\*"
  ${DoWhile} $2 != ""
    ${If} $2 != "."
    ${AndIf} $2 != ".."
      StrCpy $3 "1"
      ${Break}
    ${EndIf}
    FindNext $1 $2
  ${Loop}
  FindClose $1
  ${If} $3 == "1"
    ClearErrors
    FileOpen $QronosHandle "$QronosInstall\${QRONOS_MANIFEST}" r
    ${If} ${Errors}
      StrCpy $QronosLine ""
    ${Else}
      FileReadUTF16LE $QronosHandle $QronosLine
      FileClose $QronosHandle
      !insertmacro QRONOS_TRIM_LINE
    ${EndIf}
    ${If} $QronosLine != "${QRONOS_MANIFEST_HEADER}"
      StrCpy $QronosMessage "The chosen folder already contains files and is not a Qronos installation. Choose an empty folder or the folder of an existing Qronos installation."
    ${EndIf}
  ${EndIf}
qronos_check_done:
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; $QronosSegment (one name) -> $QronosMessage set unless Windows keeps it as written
; and it is not a device name. Same rules as build_support.install_manifest.check_entry.
Function ${UN}QronosCheckSegment
  Push $1
  Push $2
  Push $3
  StrLen $1 $QronosSegment
  StrCpy $2 $QronosSegment 1
  StrCpy $3 $QronosSegment 1 -1
  ${If} $1 = 0
  ${OrIf} $2 == " "
  ${OrIf} $3 == " "
  ${OrIf} $3 == "."
    Goto qronos_segment_bad
  ${EndIf}
  ; The device check looks at the part before the first dot, without trailing spaces.
  StrCpy $2 0
  StrCpy $3 $QronosSegment
  ${DoWhile} $2 < $1
    StrCpy $QronosChar $QronosSegment 1 $2
    ${If} $QronosChar == "."
      StrCpy $3 $QronosSegment $2
      ${Break}
    ${EndIf}
    IntOp $2 $2 + 1
  ${Loop}
  ${Do}
    StrCpy $QronosChar $3 1 -1
    ${If} $QronosChar != " "
      ${Break}
    ${EndIf}
    StrCpy $3 $3 -1
  ${Loop}
  ${If} $3 == "CON"
  ${OrIf} $3 == "PRN"
  ${OrIf} $3 == "AUX"
  ${OrIf} $3 == "NUL"
  ${OrIf} $3 == "CONIN$$"
  ${OrIf} $3 == "CONOUT$$"
    Goto qronos_segment_bad
  ${EndIf}
  StrLen $1 $3
  StrCpy $2 $3 3
  StrCpy $QronosChar $3 1 3
  ${If} $1 = 4
    ${If} $2 == "COM"
    ${OrIf} $2 == "LPT"
      ${If} $QronosChar == "0"
      ${OrIf} $QronosChar == "1"
      ${OrIf} $QronosChar == "2"
      ${OrIf} $QronosChar == "3"
      ${OrIf} $QronosChar == "4"
      ${OrIf} $QronosChar == "5"
      ${OrIf} $QronosChar == "6"
      ${OrIf} $QronosChar == "7"
      ${OrIf} $QronosChar == "8"
      ${OrIf} $QronosChar == "9"
      ${OrIf} $QronosChar == "${U+00B9}"
      ${OrIf} $QronosChar == "${U+00B2}"
      ${OrIf} $QronosChar == "${U+00B3}"
        Goto qronos_segment_bad
      ${EndIf}
    ${EndIf}
  ${EndIf}
  Goto qronos_segment_done
qronos_segment_bad:
  StrCpy $QronosMessage "The previous installation's file list contains an unsafe entry: $QronosPath"
qronos_segment_done:
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; $QronosPath (a manifest entry) -> $QronosMessage set unless it is a plain relative
; path: no drive, no leading, trailing or doubled backslash, no '..' anywhere, none of
; : / * ? " < > | (wildcards would make Delete remove unlisted files), no tab, CR or LF
; (other control characters cannot occur in a Windows file name, so a Delete naming
; one matches nothing), and every name kept as written and not a device name.
Function ${UN}QronosCheckEntry
  Push $1
  Push $2
  Push $3
  Push $4
  StrLen $2 $QronosPath
  StrCpy $3 $QronosPath 1
  StrCpy $4 $QronosPath 1 -1
  ${If} $2 = 0
  ${OrIf} $3 == "\"
  ${OrIf} $4 == "\"
    Goto qronos_entry_bad
  ${EndIf}
  StrCpy $1 0
  StrCpy $QronosSegment ""
  StrCpy $4 ""
  ${DoWhile} $1 <= $2
    ${If} $1 = $2
      StrCpy $3 "\"
    ${Else}
      StrCpy $3 $QronosPath 1 $1
    ${EndIf}
    ${If} $3 == "."
    ${AndIf} $4 == "."
      Goto qronos_entry_bad
    ${EndIf}
    ${If} $3 == "\"
      Call ${UN}QronosCheckSegment
      ${If} $QronosMessage != ""
        Goto qronos_entry_done
      ${EndIf}
      StrCpy $QronosSegment ""
    ${Else}
      ${If} $3 == ":"
      ${OrIf} $3 == "/"
      ${OrIf} $3 == "*"
      ${OrIf} $3 == "?"
      ${OrIf} $3 == '"'
      ${OrIf} $3 == "<"
      ${OrIf} $3 == ">"
      ${OrIf} $3 == "|"
      ${OrIf} $3 == "$\t"
      ${OrIf} $3 == "$\r"
      ${OrIf} $3 == "$\n"
        Goto qronos_entry_bad
      ${EndIf}
      StrCpy $QronosSegment "$QronosSegment$3"
    ${EndIf}
    StrCpy $4 $3
    IntOp $1 $1 + 1
  ${Loop}
  Goto qronos_entry_done
qronos_entry_bad:
  StrCpy $QronosMessage "The previous installation's file list contains an unsafe entry: $QronosPath"
qronos_entry_done:
  Pop $4
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; $QronosPath (a checked entry) of kind $QronosLine ("F " or "D ") -> $QronosMessage
; set if any folder on its way under $INSTDIR, or the entry itself, is a link or
; junction, or if it exists as the other kind. Checked from the disk for every entry,
; whatever "D" lines the list contains.
Function ${UN}QronosCheckOnDisk
  Push $1
  Push $2
  Push $3
  Push $4
  StrLen $2 $QronosPath
  StrCpy $1 1
  ${DoWhile} $1 <= $2
    StrCpy $4 ""
    ${If} $1 = $2
      StrCpy $3 $QronosPath
      StrCpy $4 "1"
    ${Else}
      StrCpy $3 $QronosPath 1 $1
      ${If} $3 == "\"
        StrCpy $3 $QronosPath $1
        StrCpy $4 "1"
      ${EndIf}
    ${EndIf}
    ${If} $4 == "1"
      System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\$3") i .r4'
      ${If} $4 <> -1
        IntOp $4 $4 & 0x400
        ${If} $4 <> 0
          StrCpy $QronosMessage "The previously installed path $3 is a link to another location."
          Goto qronos_disk_done
        ${EndIf}
      ${EndIf}
    ${EndIf}
    IntOp $1 $1 + 1
  ${Loop}
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\$QronosPath") i .r4'
  ${If} $4 <> -1
    IntOp $4 $4 & 0x10
    ${If} $QronosLine == "F "
    ${AndIf} $4 <> 0
      StrCpy $QronosMessage "The previous installation's file list names $QronosPath as a file, but it is a folder."
    ${ElseIf} $QronosLine == "D "
    ${AndIf} $4 = 0
      StrCpy $QronosMessage "The previous installation's file list names $QronosPath as a folder, but it is a file."
    ${EndIf}
  ${EndIf}
qronos_disk_done:
  Pop $4
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; Deletes the payload the previous installation's manifest lists -> $QronosMessage.
Function ${UN}QronosRemoveOwnedPayload
  Push $1
  Push $2
  StrCpy $QronosMessage ""
  IfFileExists "$INSTDIR\${QRONOS_MANIFEST}" 0 qronos_remove_done
  ; The install folder itself must be reached without a link or junction.
  StrCpy $QronosPath $INSTDIR
  Call ${UN}QronosCanonical
  ${If} $QronosPath == ""
  ${OrIf} $QronosPath != $QronosLexical
    StrCpy $QronosMessage "The install folder is reached through a link or junction; nothing was removed."
    Goto qronos_remove_done
  ${EndIf}

  ; Pass 1: the whole list is checked before anything is deleted: every line is a
  ; plain entry, and nothing on the way to any entry is a link or the other kind.
  ClearErrors
  FileOpen $QronosHandle "$INSTDIR\${QRONOS_MANIFEST}" r
  ${If} ${Errors}
    StrCpy $QronosMessage "The previous installation's file list cannot be read."
    Goto qronos_remove_done
  ${EndIf}
  FileReadUTF16LE $QronosHandle $QronosLine
  !insertmacro QRONOS_TRIM_LINE
  ${If} $QronosLine != "${QRONOS_MANIFEST_HEADER}"
    FileClose $QronosHandle
    StrCpy $QronosMessage "The previous installation's file list is not a Qronos file list."
    Goto qronos_remove_done
  ${EndIf}
  ${Do}
    ClearErrors
    FileReadUTF16LE $QronosHandle $QronosLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    !insertmacro QRONOS_TRIM_LINE
    StrCpy $1 $QronosLine 2
    StrCpy $QronosPath $QronosLine "" 2
    ${If} $1 != "F "
    ${AndIf} $1 != "D "
      StrCpy $QronosMessage "The previous installation's file list contains an invalid line."
      ${Break}
    ${EndIf}
    Call ${UN}QronosCheckEntry
    ${If} $QronosMessage != ""
      ${Break}
    ${EndIf}
    StrCpy $QronosLine $1
    Call ${UN}QronosCheckOnDisk
    ${If} $QronosMessage != ""
      ${Break}
    ${EndIf}
  ${Loop}
  FileClose $QronosHandle
  ${If} $QronosMessage != ""
    Goto qronos_remove_done
  ${EndIf}

  ; Pass 2: delete listed files, then remove listed folders that are now empty.
  FileOpen $QronosHandle "$INSTDIR\${QRONOS_MANIFEST}" r
  FileReadUTF16LE $QronosHandle $QronosLine
  ${Do}
    ClearErrors
    FileReadUTF16LE $QronosHandle $QronosLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    !insertmacro QRONOS_TRIM_LINE
    StrCpy $1 $QronosLine 2
    StrCpy $QronosPath $QronosLine "" 2
    ; Checked again right before each removal, in case something changed meanwhile.
    StrCpy $QronosLine $1
    Call ${UN}QronosCheckOnDisk
    ${If} $QronosMessage != ""
      ${Break}
    ${EndIf}
    ${If} $1 == "F "
      ${If} $QronosPath != "${QRONOS_MANIFEST}"
        ClearErrors
        Delete "$INSTDIR\$QronosPath"
        ${If} ${Errors}
          StrCpy $QronosMessage "The previously installed file $QronosPath could not be removed. Close Qronos and try again."
          ${Break}
        ${EndIf}
      ${EndIf}
    ${Else}
      RMDir "$INSTDIR\$QronosPath"
    ${EndIf}
  ${Loop}
  FileClose $QronosHandle
  ${If} $QronosMessage == ""
    Delete "$INSTDIR\${QRONOS_MANIFEST}"
  ${EndIf}
qronos_remove_done:
  Pop $2
  Pop $1
FunctionEnd

!macroend

; Removes a trailing CR/LF from $QronosLine.
!macro QRONOS_TRIM_LINE
  StrCpy $QronosChar $QronosLine 1 -1
  ${If} $QronosChar == "$\n"
    StrCpy $QronosLine $QronosLine -1
  ${EndIf}
  StrCpy $QronosChar $QronosLine 1 -1
  ${If} $QronosChar == "$\r"
    StrCpy $QronosLine $QronosLine -1
  ${EndIf}
!macroend

!insertmacro QRONOS_INSTALL_ROOT_FUNCTIONS ""
!insertmacro QRONOS_INSTALL_ROOT_FUNCTIONS "un."

; Called while the person chooses the folder: an unsafe folder keeps "Install"
; disabled, so it is refused before Tauri's SetOutPath creates anything. Silent
; installs skip that page; NSIS_HOOK_PREINSTALL runs the same check before any file
; is written (by then Tauri may have created the empty folder).
Function .onVerifyInstDir
  Call QronosCheckInstallRoot
  ${If} $QronosMessage != ""
    Abort
  ${EndIf}
FunctionEnd
