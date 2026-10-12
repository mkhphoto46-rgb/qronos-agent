# Qronos NSIS installer template: provenance

`installer.nsi` is Tauri's own NSIS installer template with a small, reviewed set of Qronos changes.
`tauri.conf.json` selects it (`bundle.windows.nsis.template`), and `build_support.build_desktop` refuses
to build with anything else.

## Upstream

| | |
| --- | --- |
| Project | tauri-bundler 2.9.4 (the bundler inside `@tauri-apps/cli` 2.11.4 that Qronos builds with) |
| Repository | https://github.com/tauri-apps/tauri |
| Tag | `tauri-bundler-v2.9.4`, commit `8909f221d1515955fc843808032bdc5d62209c96` |
| File | `crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi` |
| SHA-256 | `20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079` (kept byte-exact in `upstream/installer.nsi`) |
| Licence | `Apache-2.0 OR MIT`, from `crates/tauri-bundler/Cargo.toml`. Both licence texts are kept byte-exact: `License_MIT.md` (SHA-256 `86fc515b…250b`) and `License_Apache.md` (SHA-256 `a0746c84…097e`). |

Each file was fetched read-only with:

```
gh api "repos/tauri-apps/tauri/contents/<path>?ref=8909f221d1515955fc843808032bdc5d62209c96" \
   -H "Accept: application/vnd.github.raw"
```

The 10-08 packaging build confirms that this is the template the build used: its generated `utils.nsh` is
identical to upstream 2.9.4.

## The Qronos changes

`qronos-template.diff` is the complete change: a unified diff from `upstream/installer.nsi` to
`installer.nsi`. `tests/test_installer_template.py` recomputes it and requires it to match exactly.
`.gitattributes` (`* -text`) keeps the bytes exact. Every changed line carries a `Qronos` comment.

1. **The Qronos hooks are required.** After the hooks include there is an `!error` unless
   `QRONOS_TEMPLATE_HOOKS` is defined, so the template compiles only with the Qronos hook wrapper.
2. **The fence comes before any change.** `Section Install` runs `NSIS_HOOK_PREINSTALL` first, and
   `SetOutPath $INSTDIR` follows it (upstream has them the other way round). PREINSTALL takes the update fence
   and the exclusive store admission and checks the install folder before anything else happens: before the
   folder is created, before the app is stopped, and before the WebView2 child, the removal of the previous
   files, any copy or any registration.
3. **No previous uninstaller is ever run.** `PageLeaveReinstall` no longer has any `ExecWait`:
   - a WiX/MSI registration is refused visibly (`MessageBox`, then `Quit`);
   - the GUI "uninstall first" choice is refused visibly (`MessageBox`, then `Abort` back to the page);
   - passive mode and `/UPDATE` continue in place.
   Replacement is in place only, through the current installation's own file list
   (`QronosRemoveOwnedPayload`). That means no assumption that an old uninstaller holds the fence, and no fence
   deadlock between parent and child.
4. **Install success is explicit.** In a silent install, NSIS 3.11 lets a failed `File`, `WriteUninstaller`,
   `WriteRegStr` or `CreateShortcut` continue with only the error flag set, and `${GetSize}` clears that flag
   (audited; see `work/installer-logs/r11-error-flag-audit.log`). So the template now:
   - runs `ClearErrors` right after PREINSTALL;
   - checks `!insertmacro QRONOS_STEP "<step>"` IMMEDIATELY after every material statement (it aborts on the
     error flag). This includes every iteration of the resource-folder, resource-file, binary and deep-link
     loops, and every single registry write, including the INSTALLMODE=both one.
   - clears the flag only right after a checked step, and only after commands that are not install steps
     (the running-app check, a missing old-binary value, the size estimate);
   - refuses file associations with `!error` (none are configured; their macros read the registry);
   - runs `!insertmacro QRONOS_INSTALL_COMPLETE` just before `NSIS_HOOK_POSTINSTALL`.
   The update receipt is written only after that last step.
5. **No deletion by a registry-named path.** Upstream deletes `$INSTDIR\<MainBinaryName from the registry>`,
   which allows traversal or a wildcard through a registry string. That deletion is removed. The value is
   still read, for the shortcut migration only. The previous program is removed only through the owned file
   list.
6. **The uninstaller deletes only what the owned list names.** `Section Uninstall` starts with
   `NSIS_HOOK_PREUNINSTALL`, which runs, in order:
   - the fence;
   - the protected install-root check;
   - the whole-list validation against the disk;
   - stopping the app;
   - removing exactly the listed files and folders.
   The upstream raw deletion of the main program, the resources, the binaries and the resource folders (and
   `RMDir $INSTDIR`) is removed; `uninstall.exe` is still deleted by its exact name.
7. **No recursive app-data removal.** The "Delete the application data" checkbox (`RmDir /r` of
   `%APPDATA%\<identifier>` and `%LOCALAPPDATA%\<identifier>`) is not offered. Its two callbacks are left
   unreferenced, and the two `RmDir /r` lines are removed.

Everything else is upstream byte for byte. `build_support.build_desktop` checks all of the above in Tauri's
REAL generated `installer.nsi` (`verify_generated_order`).

## Updating

To move to a newer tauri-bundler:
1. Fetch the new upstream file the same way.
2. Replace `upstream/installer.nsi`.
3. Re-apply the seven changes.
4. Regenerate `qronos-template.diff`.
5. Update the hashes here and in the tests, and have the diff reviewed.
