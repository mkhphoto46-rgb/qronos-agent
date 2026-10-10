; Qronos update transaction: a failed update leaves the previously installed program usable
; (docs/offline-windows-installer.md, "Update transaction"). Included by webview2-offline.nsh, after
; qronos-install-root.nsh and qronos-update-fence.nsh, and used ONLY with the pinned Qronos template.
;
; The folder $INSTDIR\.qronos-update (T) is created by this installer after the update fence, the exclusive
; store admission and every install-root check. Its journal T\journal records, BEFORE each action takes
; effect (or, for a claim, right after the creating handle proved it), the exact NTFS identity (volume serial
; and file index) this installer observed:
;   O <id> <entry>  a previously installed listed file, about to be moved to T\old\<entry>
;   K <entry>       a folder of the new payload that did not exist and is about to be created
;   N <id> <entry>  a new payload file this installer CLAIMED (CreateFileW CREATE_NEW; identity from that handle)
;   W               all claims done: the template's writes may begin
;   C               committed: every install step passed (written at QRONOS_INSTALL_COMPLETE)
;
; * Normal failure (any QRONOS_STEP after the claims, a failure while setting aside or claiming, the commit
;   itself, or any other Abort through .onInstFailed): ROLLBACK, then Abort, so no receipt is written.
;   Rollback deletes only "N" files whose CURRENT identity is the journaled one, removes only empty "K"
;   folders, and moves every "O" file back by handle, only when its identity matches and its place is free.
;   In the same run it also restores the Apps & features values, the install-location value and the
;   previous Start-menu and desktop shortcut bytes snapshotted before anything changed.
; * Commit: the "C" line is the commit point. Then the set-aside files are deleted, each only if its
;   identity still matches, and T is removed. A failure after "C" keeps the completed new installation; what is
;   left in T then has to be removed by hand before the next update (the next installer refuses a non-empty T).
;   The update receipt is written only after the commit (NSIS_HOOK_POSTINSTALL):
;   without it the start-up barrier reports the update as unconfirmed, never as confirmed.
; * Trust: THIS run's journal handle stays open, shared for reading only, from its creation to the commit or
;   rollback, so no other program can change it; the rollback and the discard act only for this run's journal
;   (and the rollback refuses to delete an "N" file without that handle).
; * Interrupted work (a crash or kill): a T left by an EARLIER run is UNTRUSTED. A journal header can be written
;   by anyone, file identities are observable and a file list can be copied, so none of it proves what that
;   installer owned. The next installer therefore never acts on it: an empty T is removed; anything else REFUSES
;   with nothing moved or deleted, and says where the previous version's kept files are (T\old, own paths).
;   Recovery of such a folder is a manual step (restore the kept files or a backup, or reinstall elsewhere).
; * Commit also requires the installed file list to equal this installer's own list (no unclaimed write).
; * Every mutation re-checks that nothing on the way is a link (QronosCheckOnDisk) and acts through a handle
;   whose identity it compares: delete = FileDispositionInfo, move = FileRenameInfo without replace. Folders
;   and reparse points cannot be opened by these handles (no FILE_FLAG_BACKUP_SEMANTICS; attributes checked).
; * Unlisted files are never moved, overwritten or deleted: a new payload path that is already occupied
;   refuses the update before anything new is written.
; Not claimed: atomicity across power loss (each step is one NTFS operation, but this is not tested with power
; loss); automatic recovery after a crash (refused, manual); registry restore after a crash; removal of a
; shortcut the failed run newly created (it names the same program path).
;
; A probe harness may define QRONOS_PROBE_COMMIT_FAULT (the commit line is written to a handle that is not open,
; a real write failure), QRONOS_PROBE_LOCK_AFTER_CLAIM "<entry>" (this process opens that claimed file without sharing,
; so the template's real File write fails) and QRONOS_PROBE_TAMPER_KEPT "<entry>" (that kept file is replaced by a
; different file while the update runs); build_desktop never does (verify_hook_wrapper refuses them).

!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define QRONOS_UPDATE_TRANSACTION

Var QronosTxn          ; "1" while THIS run's transaction is active (from creating T until commit or rollback)
Var QronosTxnDir       ; $INSTDIR\.qronos-update
Var QronosTxnJournal   ; the journal handle while BEGIN appends ("" otherwise)
Var QronosTxnRead      ; the journal handle while a pass reads it
Var QronosTxnId        ; QronosTxnIdentity's result: "serial:high:low", "" when not a plain openable file
Var QronosTxnWant      ; the identity an action must find
Var QronosTxnSrc       ; full path an action works on
Var QronosTxnDst       ; full path a move goes to ("" = delete)
Var QronosTxnKind      ; the kind of the journal line just read ("" at the end)
Var QronosTxnCommitted ; "1" when the journal holds "C"
Var QronosTxnSaved     ; scratch (QronosTxnPruneUp's stop folder)
Var QronosTxnEntry     ; the entry QronosTxnSetAsideOne works on
Var QronosTxnLock      ; probe only: the handle QRONOS_PROBE_LOCK_AFTER_CLAIM holds
Var QronosUninstKey    ; the template's UNINSTKEY / MANUPRODUCTKEY and shortcut paths, set in NSIS_HOOK_PREINSTALL
Var QronosManuKey
Var QronosLnkStart
Var QronosLnkDesktop

; ------------------------------------------------------------------ native, identity-bound primitives

; $QronosTxnSrc (full path) -> $QronosTxnId: "serial:high:low" of a plain file opened without following a
; link, or "" when it is absent, a folder, a reparse point or cannot be opened.
Function QronosTxnIdentity
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  StrCpy $QronosTxnId ""
  ; FILE_READ_ATTRIBUTES, share all, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT (no BACKUP_SEMANTICS: no folder)
  System::Call 'kernel32::CreateFileW(w "$QronosTxnSrc", i 0x80, i 7, p 0, i 3, i 0x00200000, p 0) p .r1'
  ${If} $1 <> -1
    System::Call '*(&i52) p .r2'
    System::Call 'kernel32::GetFileInformationByHandle(p r1, p r2) i .r3'
    ${If} $3 <> 0
      System::Call '*$2(i .r4, l, l, l, i .r5, i, i, i, i .r6, i .r7)'
      IntOp $3 $4 & 0x410
      ${If} $3 = 0
        StrCpy $QronosTxnId "$5:$6:$7"
      ${EndIf}
    ${EndIf}
    System::Free $2
    System::Call 'kernel32::CloseHandle(p r1)'
  ${EndIf}
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; Deletes ($QronosTxnDst "") or moves (to $QronosTxnDst, never replacing) $QronosTxnSrc through ONE handle,
; only if that handle's file is a plain file with identity $QronosTxnWant -> $QronosMessage.
Function QronosTxnByHandle
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  Push $8
  ; DELETE | FILE_READ_ATTRIBUTES, share all, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT
  System::Call 'kernel32::CreateFileW(w "$QronosTxnSrc", i 0x10080, i 7, p 0, i 3, i 0x00200000, p 0) p .r1'
  ${If} $1 = -1
    StrCpy $QronosMessage "Qronos could not open $QronosTxnSrc for the update (another program may be using it). Nothing further was changed."
    Goto qronos_byhandle_done
  ${EndIf}
  System::Call '*(&i52) p .r2'
  System::Call 'kernel32::GetFileInformationByHandle(p r1, p r2) i .r3'
  StrCpy $8 ""
  ${If} $3 <> 0
    System::Call '*$2(i .r4, l, l, l, i .r5, i, i, i, i .r6, i .r7)'
    IntOp $3 $4 & 0x410
    ${If} $3 = 0
      StrCpy $8 "$5:$6:$7"
    ${EndIf}
  ${EndIf}
  System::Free $2
  ${If} $8 == ""
  ${OrIf} $8 != $QronosTxnWant
    System::Call 'kernel32::CloseHandle(p r1)'
    StrCpy $QronosMessage "$QronosTxnSrc is not the file this update recorded, so it was left untouched. Nothing further was changed."
    Goto qronos_byhandle_done
  ${EndIf}
  ${If} $QronosTxnDst == ""
    System::Call '*(&i1 1) p .r2'                                     ; FILE_DISPOSITION_INFO { TRUE }
    System::Call 'kernel32::SetFileInformationByHandle(p r1, i 4, p r2, i 1) i .r3'
  ${Else}
    ; FILE_RENAME_INFO (32-bit installer): ReplaceIfExists FALSE (4 bytes), RootDirectory NULL, FileNameLength,
    ; FileName (full path).
    StrLen $5 $QronosTxnDst
    IntOp $6 $5 * 2
    IntOp $5 $5 + 1
    IntOp $7 $6 + 14
    System::Call '*(i 0, p 0, i r6, &w$5 "$QronosTxnDst") p .r2'
    System::Call 'kernel32::SetFileInformationByHandle(p r1, i 3, p r2, i r7) i .r3'
  ${EndIf}
  System::Free $2
  System::Call 'kernel32::CloseHandle(p r1)'
  ${If} $3 = 0
    StrCpy $QronosMessage "Qronos could not move or remove $QronosTxnSrc for the update. Nothing further was changed."
  ${EndIf}
qronos_byhandle_done:
  Pop $8
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; $QronosPath (an entry relative to $INSTDIR, already checked by QronosCheckEntry) -> $QronosMessage set when
; a folder on the way, or the entry itself, is a link (the kind is not checked here).
Function QronosTxnLinkFree
  Push $QronosLine
  StrCpy $QronosLine ""
  Call QronosCheckOnDisk
  Pop $QronosLine
FunctionEnd

; Creates the parent folder of $QronosTxnDst and checks it is a plain folder -> $QronosMessage.
Function QronosTxnParentOf
  Push $1
  Push $2
  ${GetParent} "$QronosTxnDst" $1
  ClearErrors
  CreateDirectory "$1"
  System::Call 'kernel32::GetFileAttributesW(w "$1") i .r2'
  ${If} $2 = -1
    StrCpy $QronosMessage "Qronos could not create the folder $1 for the update. Nothing further was changed."
  ${Else}
    IntOp $2 $2 & 0x410
    ${If} $2 <> 0x10
      StrCpy $QronosMessage "$1 is not a plain folder, so the update stopped. Nothing further was changed."
    ${EndIf}
  ${EndIf}
  Pop $2
  Pop $1
FunctionEnd

; Removes the empty folders between the parent of $QronosTxnSrc and $QronosTxnSaved (exclusive), bottom up,
; never recursively (a folder that is not empty stays).
Function QronosTxnPruneUp
  Push $1
  ${GetParent} "$QronosTxnSrc" $1
  ${Do}
    ${If} $1 == $QronosTxnSaved
    ${OrIf} $1 == ""
      ${Break}
    ${EndIf}
    RMDir "$1"
    ${GetParent} "$1" $1
  ${Loop}
  Pop $1
FunctionEnd

; ------------------------------------------------------------------ journal

!macro QRONOS_TXN_WRITE TEXT
  ClearErrors
  FileWriteUTF16LE $QronosTxnJournal "${TEXT}$\r$\n"
  ${If} ${Errors}
    StrCpy $QronosMessage "Qronos could not record the update in its journal. Nothing further was changed."
  ${EndIf}
!macroend

; The next line of the journal open as $QronosTxnRead -> $QronosTxnKind ("O", "N", "K", "W", "C"; "" at the
; end), $QronosTxnWant (identity of O/N), $QronosPath (entry of O/N/K); $QronosMessage set for anything else.
Function QronosTxnReadLine
  Push $1
  Push $2
  Push $3
  StrCpy $QronosTxnKind ""
  ClearErrors
  FileReadUTF16LE $QronosTxnRead $QronosLine
  ${If} ${Errors}
    Goto qronos_readline_done
  ${EndIf}
  StrCpy $1 $QronosLine 1 -1
  ${If} $1 != "$\n"
    ; A line without its line end can only be the last one, cut by an interruption while it was written:
    ; its action had not started, so it is ignored. Anything after it is not a journal.
    ClearErrors
    FileReadUTF16LE $QronosTxnRead $1
    ${IfNot} ${Errors}
      StrCpy $QronosMessage "The interrupted update's journal is damaged, so nothing was changed. Restore the folder from a backup or reinstall into an empty folder."
    ${EndIf}
    Goto qronos_readline_done
  ${EndIf}
  !insertmacro QRONOS_TRIM_LINE
  StrCpy $QronosTxnKind $QronosLine 1
  StrCpy $1 $QronosLine "" 1
  ${If} $QronosTxnKind == "W"
  ${OrIf} $QronosTxnKind == "C"
    ${If} $1 != ""
      Goto qronos_readline_bad
    ${EndIf}
    Goto qronos_readline_done
  ${EndIf}
  StrCpy $2 $1 1
  ${If} $2 != " "
    Goto qronos_readline_bad
  ${EndIf}
  StrCpy $1 $1 "" 1
  ${If} $QronosTxnKind == "K"
    StrCpy $QronosPath $1
  ${ElseIf} $QronosTxnKind == "O"
  ${OrIf} $QronosTxnKind == "N"
    ; "<serial>:<high>:<low> <entry>": decimal numbers (a minus sign allowed), exactly two colons
    StrCpy $2 0
    StrCpy $3 0
    ${Do}
      StrCpy $QronosChar $1 1 $2
      ${If} $QronosChar == " "
        ${Break}
      ${EndIf}
      ${If} $QronosChar == ""
        Goto qronos_readline_bad
      ${EndIf}
      ${If} $QronosChar == ":"
        IntOp $3 $3 + 1
      ${ElseIf} $QronosChar != "-"
        ${If} $QronosChar S< "0"
        ${OrIf} $QronosChar S> "9"
          Goto qronos_readline_bad
        ${EndIf}
      ${EndIf}
      IntOp $2 $2 + 1
    ${Loop}
    ${If} $3 <> 2
    ${OrIf} $2 < 5
      Goto qronos_readline_bad
    ${EndIf}
    StrCpy $QronosTxnWant $1 $2
    IntOp $2 $2 + 1
    StrCpy $QronosPath $1 "" $2
  ${Else}
    Goto qronos_readline_bad
  ${EndIf}
  StrCpy $QronosMessage ""
  Call QronosCheckEntry
  ${If} $QronosMessage == ""
    Goto qronos_readline_done
  ${EndIf}
qronos_readline_bad:
  StrCpy $QronosTxnKind "?"
  StrCpy $QronosMessage "The interrupted update's journal contains a line Qronos did not write, so nothing was changed. Restore the folder from a backup or reinstall into an empty folder."
qronos_readline_done:
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; Opens the journal for reading and checks its header -> $QronosMessage.
Function QronosTxnOpenJournal
  ; GENERIC_READ, sharing read and write (this run's own writer may hold it, sharing read only, so every other
  ; writer stays refused), OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT
  System::Call 'kernel32::CreateFileW(w "$QronosTxnDir\journal", i 0x80000000, i 3, p 0, i 3, i 0x00200000, p 0) p .s'
  Pop $QronosTxnRead
  ${If} $QronosTxnRead = -1
    StrCpy $QronosTxnRead ""
    StrCpy $QronosMessage "The update journal cannot be read, so nothing was changed."
    Return
  ${EndIf}
  ClearErrors
  FileReadUTF16LE $QronosTxnRead $QronosLine
  !insertmacro QRONOS_TRIM_LINE
  ${If} $QronosLine != "${QRONOS_TXN_HEADER}"
    FileClose $QronosTxnRead
    StrCpy $QronosTxnRead ""
    StrCpy $QronosMessage "The update journal is not a Qronos journal, so nothing was changed."
  ${EndIf}
FunctionEnd

; Reads the whole journal once: every line valid -> $QronosTxnCommitted, else $QronosMessage.
Function QronosTxnValidateJournal
  StrCpy $QronosTxnCommitted ""
  Call QronosTxnOpenJournal
  ${If} $QronosMessage != ""
    Return
  ${EndIf}
  ${Do}
    Call QronosTxnReadLine
    ${If} $QronosMessage != ""
    ${OrIf} $QronosTxnKind == ""
      ${Break}
    ${EndIf}
    ${If} $QronosTxnKind == "C"
      StrCpy $QronosTxnCommitted "1"
    ${EndIf}
  ${Loop}
  FileClose $QronosTxnRead
  StrCpy $QronosTxnRead ""
FunctionEnd

; ------------------------------------------------------------------ set aside, claim, rollback, discard

; Called by QronosRemoveOwnedPayload (set-aside mode) for one validated listed file $QronosPath: its identity
; is journaled, then it is moved by handle to T\old\<entry> -> $QronosMessage.
Function QronosTxnSetAsideOne
  Push $0
  StrCpy $QronosTxnEntry $QronosPath
  StrCpy $QronosTxnSrc "$INSTDIR\$QronosTxnEntry"
  Call QronosTxnIdentity
  ${If} $QronosTxnId == ""
    System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnSrc") i .r0'
    ${If} $0 <> -1
      StrCpy $QronosMessage "The previously installed file $QronosTxnEntry could not be removed from its place for the update: it is not a plain file. Nothing further was changed."
    ${EndIf}
    Goto qronos_setaside_done  ; a listed file that is not there: nothing to keep
  ${EndIf}
  StrCpy $QronosTxnWant $QronosTxnId
  StrCpy $QronosPath "${QRONOS_TXN_FOLDER}\old\$QronosTxnEntry"
  Call QronosTxnLinkFree
  ${If} $QronosMessage != ""
    Goto qronos_setaside_done
  ${EndIf}
  StrCpy $QronosTxnDst "$INSTDIR\$QronosPath"
  System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnDst") i .r0'
  ${If} $0 <> -1
    StrCpy $QronosMessage "The update's folder already holds $QronosPath, so nothing further was changed."
    Goto qronos_setaside_done
  ${EndIf}
  !insertmacro QRONOS_TXN_WRITE "O $QronosTxnWant $QronosTxnEntry"
  ${If} $QronosMessage != ""
    Goto qronos_setaside_done
  ${EndIf}
  Call QronosTxnParentOf
  ${If} $QronosMessage == ""
    Call QronosTxnByHandle
  ${EndIf}
  ${If} $QronosMessage != ""
    StrCpy $QronosMessage "The previously installed file $QronosTxnEntry could not be removed from its place for the update (it is kept). Close Qronos and try again."
  ${EndIf}
qronos_setaside_done:
  StrCpy $QronosPath $QronosTxnEntry
  Pop $0
FunctionEnd

; Claims every folder and file the new payload list names ($PLUGINSDIR\qronos-new.list) -> $QronosMessage.
Function QronosTxnClaimNew
  Push $0
  Push $1
  Push $2
  ClearErrors
  FileOpen $QronosHandle "$PLUGINSDIR\qronos-new.list" r
  ${If} ${Errors}
    StrCpy $QronosMessage "This installer's own file list cannot be read; nothing was installed."
    Goto qronos_claim_done
  ${EndIf}
  ; Pass 1: folders that do not exist yet are journaled ("K") before any is created. The list names every
  ; folder, deepest first, so a rollback removes them in that order.
  Call QronosCheckListHeader
  ${Do}
    ${If} $QronosMessage != ""
      ${Break}
    ${EndIf}
    ClearErrors
    FileReadUTF16LE $QronosHandle $QronosLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    Call QronosCheckListLine
    ${If} $QronosMessage == ""
    ${AndIf} $QronosLine == "D "
      System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\$QronosPath") i .r1'
      ${If} $1 = -1
        !insertmacro QRONOS_TXN_WRITE "K $QronosPath"
      ${EndIf}
    ${EndIf}
  ${Loop}
  ; Pass 2: the folders are created and must be plain folders.
  ${If} $QronosMessage == ""
    FileSeek $QronosHandle 0 SET
    Call QronosCheckListHeader
    ${Do}
      ${If} $QronosMessage != ""
        ${Break}
      ${EndIf}
      ClearErrors
      FileReadUTF16LE $QronosHandle $QronosLine
      ${If} ${Errors}
        ${Break}
      ${EndIf}
      Call QronosCheckListLine
      ${If} $QronosMessage == ""
      ${AndIf} $QronosLine == "D "
        CreateDirectory "$INSTDIR\$QronosPath"
        Call QronosCheckOnDisk
        System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\$QronosPath") i .r1'
        ${If} $1 = -1
          StrCpy $QronosMessage "Qronos could not create the folder $QronosPath; nothing was installed."
        ${EndIf}
      ${EndIf}
    ${Loop}
  ${EndIf}
  ; Pass 3: every file is claimed: created new by this installer (never opened if anything exists there),
  ; and its identity journaled from that handle. An occupied path refuses: unlisted files stay untouched.
  ${If} $QronosMessage == ""
    FileSeek $QronosHandle 0 SET
    Call QronosCheckListHeader
    ${Do}
      ${If} $QronosMessage != ""
        ${Break}
      ${EndIf}
      ClearErrors
      FileReadUTF16LE $QronosHandle $QronosLine
      ${If} ${Errors}
        ${Break}
      ${EndIf}
      Call QronosCheckListLine
      ${If} $QronosMessage == ""
      ${AndIf} $QronosLine == "F "
        ; FILE_READ_ATTRIBUTES, share all, CREATE_NEW, FILE_ATTRIBUTE_NORMAL
        System::Call 'kernel32::CreateFileW(w "$INSTDIR\$QronosPath", i 0x80, i 7, p 0, i 1, i 0x80, p 0) p .r1'
        ${If} $1 = -1
          StrCpy $QronosMessage "Something that is not part of the previous Qronos installation is already at $QronosPath. It was left untouched and the update stopped; the previous version was kept."
          ${Break}
        ${EndIf}
        System::Call '*(&i52) p .r2'
        System::Call 'kernel32::GetFileInformationByHandle(p r1, p r2) i .r0'
        StrCpy $QronosTxnId ""
        ${If} $0 <> 0
          Push $3
          Push $4
          Push $5
          Push $6
          System::Call '*$2(i .r3, l, l, l, i .r4, i, i, i, i .r5, i .r6)'
          StrCpy $QronosTxnId "$4:$5:$6"
          Pop $6
          Pop $5
          Pop $4
          Pop $3
        ${EndIf}
        System::Free $2
        ${If} $QronosTxnId == ""
          System::Call 'kernel32::CloseHandle(p r1)'
          StrCpy $QronosMessage "Qronos could not identify the new file $QronosPath; nothing was installed."
          ${Break}
        ${EndIf}
        !insertmacro QRONOS_TXN_WRITE "N $QronosTxnId $QronosPath"
        System::Call 'kernel32::CloseHandle(p r1)'
      ${EndIf}
    ${Loop}
  ${EndIf}
  FileClose $QronosHandle
qronos_claim_done:
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; Journal-driven rollback of an uncommitted transaction (this run's, or an interrupted one) -> $QronosMessage.
; THIS run's journal is trusted: its handle (shared for reading only) has been open since this installer created
; the journal, so no other program could change it, and its "N" files are the ones this run claimed. An earlier
; run's journal is NOT: its lines and identities could have been written by anyone, so a rollback of it never
; deletes a file outside the transaction folder; while any of its "N" files exists, it refuses.
Function QronosTxnRollback
  Push $1
  Push $2
  StrCpy $2 ""
  ${If} $QronosTxnJournal != ""
    StrCpy $2 "1"
  ${EndIf}
  StrCpy $QronosMessage ""
  Call QronosTxnValidateJournal
  ${If} $QronosMessage != ""
    Goto qronos_rollback_done
  ${EndIf}
  ${If} $QronosTxnCommitted == "1"
    StrCpy $QronosMessage "The update was already committed; it is not rolled back."
    Goto qronos_rollback_done
  ${EndIf}
  ; Pass N: new files, only while they are still exactly the files this installer claimed.
  Call QronosTxnOpenJournal
  ${Do}
    Call QronosTxnReadLine
    ${If} $QronosMessage != ""
    ${OrIf} $QronosTxnKind == ""
      ${Break}
    ${EndIf}
    ${If} $QronosTxnKind == "N"
      Call QronosTxnLinkFree
      ${If} $QronosMessage != ""
        ${Break}
      ${EndIf}
      StrCpy $QronosTxnSrc "$INSTDIR\$QronosPath"
      System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnSrc") i .r1'
      ${If} $1 <> -1
        ${If} $2 != "1"
          StrCpy $QronosMessage "It had already begun writing new files, and Qronos cannot prove which files those are (for example $QronosPath), so nothing was deleted. Restore the folder from a backup or reinstall into an empty folder."
          ${Break}
        ${EndIf}
        StrCpy $QronosTxnDst ""
        Call QronosTxnByHandle
        ${If} $QronosMessage != ""
          ${Break}
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${Loop}
  ${If} $QronosTxnRead != ""
    FileClose $QronosTxnRead
    StrCpy $QronosTxnRead ""
  ${EndIf}
  ${If} $QronosMessage != ""
    Goto qronos_rollback_done
  ${EndIf}
  ; Pass K: folders this installer created, deepest first, only when empty and not a link.
  Call QronosTxnOpenJournal
  ${Do}
    Call QronosTxnReadLine
    ${If} $QronosMessage != ""
    ${OrIf} $QronosTxnKind == ""
      ${Break}
    ${EndIf}
    ${If} $QronosTxnKind == "K"
      System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\$QronosPath") i .r1'
      ${If} $1 <> -1
        IntOp $1 $1 & 0x410
        ${If} $1 = 0x10
          RMDir "$INSTDIR\$QronosPath"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${Loop}
  ${If} $QronosTxnRead != ""
    FileClose $QronosTxnRead
    StrCpy $QronosTxnRead ""
  ${EndIf}
  ${If} $QronosMessage != ""
    Goto qronos_rollback_done
  ${EndIf}
  ; Pass O: every set-aside file back to its exact place, by handle, only if it is still that file and its
  ; place is free. A file that was never moved must still be in its place.
  Call QronosTxnOpenJournal
  ${Do}
    Call QronosTxnReadLine
    ${If} $QronosMessage != ""
    ${OrIf} $QronosTxnKind == ""
      ${Break}
    ${EndIf}
    ${If} $QronosTxnKind == "O"
      Call QronosTxnLinkFree
      ${If} $QronosMessage != ""
        ${Break}
      ${EndIf}
      StrCpy $QronosTxnDst "$INSTDIR\$QronosPath"
      StrCpy $QronosTxnSrc "$QronosTxnDir\old\$QronosPath"
      Push $QronosPath
      StrCpy $QronosPath "${QRONOS_TXN_FOLDER}\old\$QronosPath"
      Call QronosTxnLinkFree
      Pop $QronosPath
      ${If} $QronosMessage != ""
        ${Break}
      ${EndIf}
      System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnSrc") i .r1'
      ${If} $1 = -1
        Push $QronosTxnSrc
        StrCpy $QronosTxnSrc $QronosTxnDst
        Call QronosTxnIdentity
        Pop $QronosTxnSrc
        ${If} $QronosTxnId != $QronosTxnWant
          StrCpy $QronosMessage "The previously installed file $QronosPath is missing, so the previous version could not be completely restored. Nothing further was changed."
          ${Break}
        ${EndIf}
      ${Else}
        System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnDst") i .r1'
        ${If} $1 <> -1
          StrCpy $QronosMessage "Something is already at $QronosPath, so the previous version could not be completely restored. It was left untouched."
          ${Break}
        ${EndIf}
        Call QronosTxnParentOf
        ${If} $QronosMessage == ""
          Call QronosTxnByHandle
        ${EndIf}
        ${If} $QronosMessage != ""
          ${Break}
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${Loop}
  ${If} $QronosTxnRead != ""
    FileClose $QronosTxnRead
    StrCpy $QronosTxnRead ""
  ${EndIf}
  ${If} $QronosMessage == ""
    Call QronosTxnFinish
  ${EndIf}
qronos_rollback_done:
  ${If} $QronosTxnRead != ""
    FileClose $QronosTxnRead
    StrCpy $QronosTxnRead ""
  ${EndIf}
  Pop $2
  Pop $1
FunctionEnd

; Committed: deletes the set-aside files, each only if it is still the recorded file -> $QronosMessage.
Function QronosTxnDiscard
  Push $1
  StrCpy $QronosMessage ""
  Call QronosTxnOpenJournal
  ${Do}
    ${If} $QronosMessage != ""
      ${Break}
    ${EndIf}
    Call QronosTxnReadLine
    ${If} $QronosMessage != ""
    ${OrIf} $QronosTxnKind == ""
      ${Break}
    ${EndIf}
    ${If} $QronosTxnKind == "O"
      Push $QronosPath
      StrCpy $QronosPath "${QRONOS_TXN_FOLDER}\old\$QronosPath"
      Call QronosTxnLinkFree
      Pop $QronosPath
      StrCpy $QronosTxnSrc "$QronosTxnDir\old\$QronosPath"
      ${If} $QronosMessage == ""
        System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnSrc") i .r1'
        ${If} $1 <> -1
          StrCpy $QronosTxnDst ""
          Call QronosTxnByHandle
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${Loop}
  ${If} $QronosTxnRead != ""
    FileClose $QronosTxnRead
    StrCpy $QronosTxnRead ""
  ${EndIf}
  ${If} $QronosMessage == ""
    Call QronosTxnFinish
  ${EndIf}
  Pop $1
FunctionEnd

; After a complete rollback or discard: removes the empty set-aside folders, the journal and T, never
; recursively -> $QronosMessage when T still holds something unexpected.
Function QronosTxnFinish
  ${If} $QronosTxnJournal != ""
    FileClose $QronosTxnJournal  ; this run's protected handle goes only now, right before the journal is removed
    StrCpy $QronosTxnJournal ""
  ${EndIf}
  Call QronosTxnOpenJournal
  StrCpy $QronosTxnSaved "$QronosTxnDir\old"
  ${If} $QronosMessage == ""
    ${Do}
      Call QronosTxnReadLine
      ${If} $QronosMessage != ""
      ${OrIf} $QronosTxnKind == ""
        ${Break}
      ${EndIf}
      ${If} $QronosTxnKind == "O"
        StrCpy $QronosTxnSrc "$QronosTxnDir\old\$QronosPath"
        Call QronosTxnPruneUp
      ${EndIf}
    ${Loop}
  ${EndIf}
  ${If} $QronosTxnRead != ""
    FileClose $QronosTxnRead
    StrCpy $QronosTxnRead ""
  ${EndIf}
  ${If} $QronosMessage != ""
    Return
  ${EndIf}
  RMDir "$QronosTxnDir\old"
  Delete "$QronosTxnDir\journal"
  RMDir "$QronosTxnDir"
  StrCpy $QronosTxn ""
  ${If} ${FileExists} "$QronosTxnDir\*.*"
    StrCpy $QronosMessage "The update folder $QronosTxnDir holds files Qronos did not put there; they were left untouched. Remove them, then run the installer again."
  ${EndIf}
FunctionEnd

; ------------------------------------------------------------------ recovery, begin, commit

; Before anything else changes: an existing T was left by an EARLIER run. Nothing in it can be proved to be
; that installer's (anyone can write a journal header, identities are observable, a file list can be copied),
; so it is never acted on: an EMPTY T is removed (nothing to lose), anything else is refused with nothing moved
; or deleted -> $QronosMessage. The previous version's kept files stay under T\old at their own paths.
Function QronosTxnRecover
  Push $1
  Push $2
  StrCpy $QronosMessage ""
  StrCpy $QronosTxnDir "$INSTDIR\${QRONOS_TXN_FOLDER}"
  System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnDir") i .r1'
  ${If} $1 = -1
    Goto qronos_recover_done
  ${EndIf}
  IntOp $1 $1 & 0x410
  ${If} $1 = 0x10
    RMDir "$QronosTxnDir"  ; non-recursive: only an empty folder goes
  ${EndIf}
  System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnDir") i .r1'
  ${If} $1 <> -1
    StrCpy $QronosMessage "An earlier Qronos update was interrupted and could not be safely finished: Qronos cannot prove what $QronosTxnDir holds, so nothing in it or in this folder was changed. The previous version's files are kept under $QronosTxnDir\old at their own paths; restore them from there or from a backup, or reinstall into an empty folder."
  ${EndIf}
qronos_recover_done:
  Pop $2
  Pop $1
FunctionEnd

; The transaction begins: T, the journal, this installer's own list, the metadata snapshot, the set-aside of
; the previous payload and the claims -> $QronosMessage (the caller rolls back on failure).
Function QronosTxnBegin
  Push $1
  StrCpy $QronosMessage ""
  StrCpy $QronosTxnDir "$INSTDIR\${QRONOS_TXN_FOLDER}"
  System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnDir") i .r1'
  ${If} $1 <> -1
    StrCpy $QronosMessage "$QronosTxnDir already exists, so the update did not start. Nothing was changed."
    Goto qronos_begin_done
  ${EndIf}
  ClearErrors
  CreateDirectory "$QronosTxnDir"
  System::Call 'kernel32::GetFileAttributesW(w "$QronosTxnDir") i .r1'
  IntOp $1 $1 & 0x410
  ${If} $1 <> 0x10
    StrCpy $QronosMessage "Qronos could not create its update folder $QronosTxnDir. Nothing was changed."
    Goto qronos_begin_done
  ${EndIf}
  ; GENERIC_WRITE, FILE_SHARE_READ, CREATE_NEW: this installer's own new journal
  System::Call 'kernel32::CreateFileW(w "$QronosTxnDir\journal", i 0x40000000, i 1, p 0, i 1, i 0x80, p 0) p .r1'
  ${If} $1 = -1
    RMDir "$QronosTxnDir"
    StrCpy $QronosMessage "Qronos could not create its update journal. Nothing was changed."
    Goto qronos_begin_done
  ${EndIf}
  StrCpy $QronosTxnJournal $1
  StrCpy $QronosTxn "1"
  !insertmacro QRONOS_TXN_WRITE "${QRONOS_TXN_HEADER}"
  ${If} $QronosMessage != ""
    Goto qronos_begin_done
  ${EndIf}
  ; This installer's own list of the files it writes (the build's install manifest).
  InitPluginsDir
  !ifdef QRONOS_OWNED_MANIFEST
    ClearErrors
    File "/oname=$PLUGINSDIR\qronos-new.list" "${QRONOS_OWNED_MANIFEST}"
    ${If} ${Errors}
      StrCpy $QronosMessage "This installer could not unpack its own file list; nothing was installed."
      Goto qronos_begin_done
    ${EndIf}
  !else
    ; NSIS_HOOK_PREINSTALL refuses to compile without it; a probe that never begins an update compiles.
    StrCpy $QronosMessage "This installer was built without its own file list; nothing was installed."
    Goto qronos_begin_done
  !endif
  Call QronosTxnSnapshotMetadata
  ; The previous payload, set aside (identity journaled, moved by handle); the list is validated first.
  StrCpy $QronosSetAside "1"
  Call QronosRemoveOwnedPayload
  StrCpy $QronosSetAside ""
  ${If} $QronosMessage != ""
    Goto qronos_begin_done
  ${EndIf}
  Call QronosTxnClaimNew
  ${If} $QronosMessage != ""
    Goto qronos_begin_done
  ${EndIf}
  !insertmacro QRONOS_TXN_WRITE "W"
  ${If} $QronosMessage != ""
    Goto qronos_begin_done
  ${EndIf}
  ; The journal handle stays open (shared for reading only) until the commit or the rollback, so no other
  ; program can change this run's journal while the update runs.
  !ifdef QRONOS_PROBE_LOCK_AFTER_CLAIM
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\${QRONOS_PROBE_LOCK_AFTER_CLAIM}", i 0x80000000, i 0, p 0, i 3, i 0, p 0) p .s'
    Pop $QronosTxnLock
  !endif
  !ifdef QRONOS_PROBE_TAMPER_KEPT
    ; probe: another program replaces a kept file while the update runs (a new file, a new identity)
    Delete "$QronosTxnDir\old\${QRONOS_PROBE_TAMPER_KEPT}"
    FileOpen $1 "$QronosTxnDir\old\${QRONOS_PROBE_TAMPER_KEPT}" w
    FileWrite $1 "replaced by another program"
    FileClose $1
  !endif
qronos_begin_done:
  Pop $1
FunctionEnd

; Commit (QRONOS_INSTALL_COMPLETE): the "C" line, then the set-aside files are discarded -> $QronosMessage set
; only when the commit itself failed (the caller rolls back).
Function QronosTxnCommit
  Push $1
  Push $2
  Push $3
  StrCpy $QronosMessage ""
  ; The installed file list must be exactly this installer's own list (the one every claim was made from):
  ; otherwise a file could have been written without a claim, or a claim never written. Compared line by line.
  ClearErrors
  FileOpen $1 "$INSTDIR\${QRONOS_MANIFEST}" r
  ${If} ${Errors}
    StrCpy $QronosMessage "The installed file list cannot be read, so the update was not committed."
    Goto qronos_commit_done
  ${EndIf}
  ClearErrors
  FileOpen $2 "$PLUGINSDIR\qronos-new.list" r
  ${If} ${Errors}
    FileClose $1
    StrCpy $QronosMessage "This installer's own file list cannot be read, so the update was not committed."
    Goto qronos_commit_done
  ${EndIf}
  ${Do}
    ClearErrors
    FileReadUTF16LE $1 $3
    StrCpy $QronosTxnSaved "0"
    ${If} ${Errors}
      StrCpy $QronosTxnSaved "1"
    ${EndIf}
    ClearErrors
    FileReadUTF16LE $2 $QronosLine
    ${If} ${Errors}
      ${If} $QronosTxnSaved != "1"
        StrCpy $QronosMessage "The installed file list does not match this installer's own list, so the update was not committed."
      ${EndIf}
      ${Break}
    ${EndIf}
    ${If} $QronosTxnSaved == "1"
    ${OrIf} $3 != $QronosLine
      StrCpy $QronosMessage "The installed file list does not match this installer's own list, so the update was not committed."
      ${Break}
    ${EndIf}
  ${Loop}
  FileClose $1
  FileClose $2
  ${If} $QronosMessage != ""
    Goto qronos_commit_done
  ${EndIf}
  ; The commit point: "C" through this run's own protected journal handle.
  ClearErrors
  !ifdef QRONOS_PROBE_COMMIT_FAULT
    FileWriteUTF16LE $QronosTxnRead "C$\r$\n"  ; probe: a write to a handle that is not open (fails for real)
    SetErrors                                  ; and the flag is guaranteed for the probe
  !else
    FileWriteUTF16LE $QronosTxnJournal "C$\r$\n"
  !endif
  ${If} ${Errors}
    StrCpy $QronosMessage "Qronos could not record the completed update."
    Goto qronos_commit_done
  ${EndIf}
  StrCpy $QronosTxn ""
  Call QronosTxnDiscard
  ${If} $QronosMessage != ""
    DetailPrint "The update is complete, but the previous version's kept files could not all be removed; remove $QronosTxnDir by hand before the next update: $QronosMessage"
    StrCpy $QronosMessage ""
  ${EndIf}
qronos_commit_done:
  Pop $3
  Pop $2
  Pop $1
FunctionEnd

; ------------------------------------------------------------------ metadata (same run only)

!macro QRONOS_TXN_META_STR KEY NAME
  ClearErrors
  ReadRegStr $1 SHCTX "${KEY}" "${NAME}"
  ${If} ${Errors}
    WriteINIStr "$PLUGINSDIR\qronos-meta.ini" "absent" "${KEY}|${NAME}" "1"
  ${Else}
    WriteINIStr "$PLUGINSDIR\qronos-meta.ini" "str" "${KEY}|${NAME}" "$1"
  ${EndIf}
!macroend

!macro QRONOS_TXN_META_DWORD KEY NAME
  ClearErrors
  ReadRegDWORD $1 SHCTX "${KEY}" "${NAME}"
  ${If} ${Errors}
    WriteINIStr "$PLUGINSDIR\qronos-meta.ini" "absent" "${KEY}|${NAME}" "1"
  ${Else}
    WriteINIStr "$PLUGINSDIR\qronos-meta.ini" "dword" "${KEY}|${NAME}" "$1"
  ${EndIf}
!macroend

!macro QRONOS_TXN_RESTORE_STR KEY NAME
  ClearErrors
  ReadINIStr $1 "$PLUGINSDIR\qronos-meta.ini" "absent" "${KEY}|${NAME}"
  ${If} $1 == "1"
    DeleteRegValue SHCTX "${KEY}" "${NAME}"
  ${Else}
    ReadINIStr $1 "$PLUGINSDIR\qronos-meta.ini" "str" "${KEY}|${NAME}"
    WriteRegStr SHCTX "${KEY}" "${NAME}" "$1"
  ${EndIf}
!macroend

!macro QRONOS_TXN_RESTORE_DWORD KEY NAME
  ClearErrors
  ReadINIStr $1 "$PLUGINSDIR\qronos-meta.ini" "absent" "${KEY}|${NAME}"
  ${If} $1 == "1"
    DeleteRegValue SHCTX "${KEY}" "${NAME}"
  ${Else}
    ReadINIStr $1 "$PLUGINSDIR\qronos-meta.ini" "dword" "${KEY}|${NAME}"
    WriteRegDWORD SHCTX "${KEY}" "${NAME}" "$1"
  ${EndIf}
!macroend

; The values the template's Install section writes, the install-location value and the shortcut bytes, as
; they were before anything changed (in this installer's private plug-ins folder).
Function QronosTxnSnapshotMetadata
  Push $1
  InitPluginsDir
  Delete "$PLUGINSDIR\qronos-meta.ini"
  !insertmacro QRONOS_TXN_META_STR "$QronosManuKey" ""
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "MainBinaryName"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "DisplayName"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "DisplayIcon"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "DisplayVersion"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "Publisher"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "InstallLocation"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "UninstallString"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "URLInfoAbout"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "URLUpdateInfo"
  !insertmacro QRONOS_TXN_META_STR "$QronosUninstKey" "HelpLink"
  !insertmacro QRONOS_TXN_META_DWORD "$QronosUninstKey" "NoModify"
  !insertmacro QRONOS_TXN_META_DWORD "$QronosUninstKey" "NoRepair"
  !insertmacro QRONOS_TXN_META_DWORD "$QronosUninstKey" "EstimatedSize"
  Delete "$PLUGINSDIR\qronos-start.lnk"
  Delete "$PLUGINSDIR\qronos-desktop.lnk"
  ${If} ${FileExists} "$QronosLnkStart"
    System::Call 'kernel32::CopyFileW(w "$QronosLnkStart", w "$PLUGINSDIR\qronos-start.lnk", i 1) i .r1'
  ${EndIf}
  ${If} ${FileExists} "$QronosLnkDesktop"
    System::Call 'kernel32::CopyFileW(w "$QronosLnkDesktop", w "$PLUGINSDIR\qronos-desktop.lnk", i 1) i .r1'
  ${EndIf}
  ClearErrors
  Pop $1
FunctionEnd

Function QronosTxnRestoreMetadata
  Push $1
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosManuKey" ""
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "MainBinaryName"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "DisplayName"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "DisplayIcon"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "DisplayVersion"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "Publisher"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "InstallLocation"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "UninstallString"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "URLInfoAbout"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "URLUpdateInfo"
  !insertmacro QRONOS_TXN_RESTORE_STR "$QronosUninstKey" "HelpLink"
  !insertmacro QRONOS_TXN_RESTORE_DWORD "$QronosUninstKey" "NoModify"
  !insertmacro QRONOS_TXN_RESTORE_DWORD "$QronosUninstKey" "NoRepair"
  !insertmacro QRONOS_TXN_RESTORE_DWORD "$QronosUninstKey" "EstimatedSize"
  ${If} ${FileExists} "$PLUGINSDIR\qronos-start.lnk"
    System::Call 'kernel32::CopyFileW(w "$PLUGINSDIR\qronos-start.lnk", w "$QronosLnkStart", i 0) i .r1'
  ${EndIf}
  ${If} ${FileExists} "$PLUGINSDIR\qronos-desktop.lnk"
    System::Call 'kernel32::CopyFileW(w "$PLUGINSDIR\qronos-desktop.lnk", w "$QronosLnkDesktop", i 0) i .r1'
  ${EndIf}
  ClearErrors
  Pop $1
FunctionEnd

; This run's transaction is undone (a failed step, a failed commit, any other Abort): the files from the
; journal, then the metadata. Reports the outcome; never claims success it did not reach.
Function QronosTxnRollbackInRun
  !ifdef QRONOS_PROBE_LOCK_AFTER_CLAIM
    ${If} $QronosTxnLock != ""
      System::Call 'kernel32::CloseHandle(p $QronosTxnLock)'
      StrCpy $QronosTxnLock ""
    ${EndIf}
  !endif
  Push $QronosMessage
  Call QronosTxnRollback
  ${If} $QronosMessage == ""
    Call QronosTxnRestoreMetadata
    DetailPrint "The update did not complete; the previous version was restored."
  ${Else}
    DetailPrint "The update did not complete and the previous version could not be completely restored: $QronosMessage"
    !ifdef QRONOS_PROBE_RESULT
      FileOpen $R7 "${QRONOS_PROBE_RESULT}.rollback" w
      FileWrite $R7 "$QronosMessage"
      FileClose $R7
    !endif
  ${EndIf}
  StrCpy $QronosTxn ""
  Pop $QronosMessage
FunctionEnd

; Called by QRONOS_STEP (qronos-update-fence.nsh) right before its Abort.
!macro QRONOS_TXN_ON_FAILURE
  ${If} $QronosTxn == "1"
    Call QronosTxnRollbackInRun
  ${EndIf}
!macroend

; Called by QRONOS_INSTALL_COMPLETE after the last step check: the commit point. A failed commit is rolled
; back and stops the installation (no receipt).
!macro QRONOS_TXN_COMMIT
  ${If} $QronosTxn == "1"
    Call QronosTxnCommit
    ${If} $QronosMessage != ""
      Call QronosTxnRollbackInRun
      StrCpy $QronosInstallComplete "0"
      !ifdef QRONOS_PROBE_RESULT
        Push "the update commit"
        Call QronosProbeStepFailed
      !endif
      Abort "Qronos was not completely installed: the update could not be committed. Run the installer again."
    ${EndIf}
  ${EndIf}
!macroend

; Any other Abort after the transaction began (for example the running-app check after SetOutPath).
Function .onInstFailed
  ${If} $QronosTxn == "1"
    Call QronosTxnRollbackInRun
  ${EndIf}
FunctionEnd
