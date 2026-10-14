# DRAFT - SignPath Foundation application for Qronos (not submitted)

> Draft only. Do not submit until every blocker in docs/signing/free-code-signing.md section 2 is closed and
> the owner has approved the text. Submitting, creating accounts and contacting SignPath are owner actions.
> The fields follow what the terms ask for. Match them to the form on https://signpath.org/ when applying.

- **Project name:** Qronos
- **Repository:** `[OWNER: public repository URL]` (currently private; must be public first)
- **Licence:** `[OWNER: OSI-approved licence chosen]` (none chosen yet). No commercial dual-licensing.
- **Description:** A Persian-first desktop assistant for Windows. Its core runs offline on the user's PC, with
  local speech recognition, voice output and local language models. `[OWNER: confirm or edit]`
- **Released in the form to be signed:** `[OWNER: URL of the first public release with the NSIS installer]`
  (no release yet)
- **Build system:** GitHub Actions on GitHub-hosted Windows runners. The unsigned installer is uploaded with
  `actions/upload-artifact` and submitted with `signpath/github-action-submit-signing-request`.
  The workflow is `docs/signing/signpath-release.workflow.yml` (actions pinned by commit SHA), to be moved into
  `.github/workflows/` after acceptance.
- **Artifacts to sign:** one PE file, `qronos-desktop_<version>_x64-setup.exe` (NSIS installer; product name
  and version are set by `desktop/src-tauri/tauri.conf.json`; it includes an uninstaller).
- **Not to be signed:** upstream binaries inside the installer: CPython, PyInstaller, NSIS, Microsoft VC++
  redistributable, Microsoft WebView2 installer, ffmpeg, whisper.cpp, crispasr.
- **Questions for SignPath before approval:**
  1. The installer embeds the Microsoft VC++ 2015-2022 redistributable and the WebView2 standalone installer,
     and the runtime ships the Microsoft C++ runtime DLLs. Are these acceptable as "System Libraries"?
  2. The backend is frozen with PyInstaller (an upstream bootloader plus project bytecode). May
     `qronos-runtime.exe` be signed as project output, or must it stay unsigned?
- **Team and roles:** `[OWNER: Authors / Reviewers / Approvers]`. `[OWNER: confirm MFA is enabled for all
  members]`
- **Code signing policy page:** `[OWNER: URL]` (draft: `docs/signing/code-signing-policy.DRAFT.md`)
- **Privacy statement:** on the policy page (draft wording there, pending the owner's confirmation and review)
