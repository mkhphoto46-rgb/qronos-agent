# Free code signing for the Qronos installer: options, eligibility and owner steps

Status, 2026-10-14: **readiness only.** Nothing has been applied for, submitted or signed. The installer is
unsigned, and Qronos has no certificate today. The verified free route found for a publicly trusted signature
on the NSIS installer is SignPath Foundation, and Qronos does not meet its terms yet (section 2). Nothing here
is legal advice; licence and terms questions need the owner's own review.

What this change adds (all opt-in, offline and fail-closed):

- `build_support/signpath_release.py`: `readiness`, `check`, `prepare` (a dry-run signing request) and
  `verify` (a returned file must be the submitted installer plus a signature that the existing Authenticode
  verifier accepts, by the configured SignPath Foundation subject).
- `config/signpath_release.template.json`: the config shape, holding no secret. Its `"enabled": false` and its
  placeholders are refused, so it never works as it is.
- `docs/signing/signpath-release.workflow.yml`: a CI template outside `.github/workflows`, so GitHub never runs it.
  It is started by hand only. Every action is pinned to a full commit SHA resolved from its upstream repository,
  and its inputs were checked against that commit's `action.yml`. Its commands and options are checked against
  the current builders by `tests/test_signpath_release.py`. The SignPath ids come only from the validated
  config, so a missing or disabled config stops the run before anything is built.
- `docs/signing/code-signing-policy.DRAFT.md` and `docs/signing/signpath-application.DRAFT.md`: drafts. They have
  not been published or submitted.

Unchanged: `config/authenticode_public_roots.json` (empty; every chain is refused), the Authenticode `check` /
`sign` / `verify` rules, the release preflight (it still opens only on
`authenticode_release.verify_signed`), and the Ed25519 update-package key, which is a separate trust path.

## 1. Options (primary sources, read 2026-10-14)

| Option | Cost | Windows trust for an NSIS .exe | Fits Qronos today | Source |
|---|---|---|---|---|
| SignPath Foundation | Free for eligible open-source projects | Yes. The certificate is issued to **SignPath Foundation**, so Windows shows SignPath Foundation as the publisher, not Qronos | No: section 2 | https://signpath.org/ , https://signpath.org/terms.html |
| Microsoft Store, MSIX | No signing cost: Microsoft signs Store MSIX packages | For the Store package only, not for a downloaded .exe | No: Qronos builds NSIS only (`build_desktop` supports `nsis`); MSIX is new packaging work | https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options |
| Microsoft Store, MSI/EXE | The installer must already be signed by a Trusted Root Program CA | Only with a paid or SignPath certificate | Depends on the above | same page |
| Azure Artifact Signing (Trusted Signing) | Paid (about USD 9.99/month, Basic) | Yes | Not free. Individuals: US and Canada only; organisations: listed countries only | https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart , https://azure.microsoft.com/en-us/pricing/details/trusted-signing/ |
| OV / EV certificate from a public CA | Paid, yearly | Yes | Not free | CA price lists |
| Certum open-source code signing | Paid (no free offer at the time of reading) | Yes | Not free | https://shop.certum.eu/code-signing.html |
| Sigstore / GitHub artifact attestations | Free | **None.** Windows does not check them | Yes, as provenance only (in the CI template) | https://docs.sigstore.dev/about/overview/ , https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds |
| Self-signed or private CA | Free | **None** for the public. The existing gate refuses self-signed and unpinned roots | No | (the gate itself) |

A publicly trusted Authenticode signature does not guarantee immediate SmartScreen reputation for a downloaded
installer; Microsoft builds reputation over time, and nothing here claims or predicts it. Store-distributed MSIX
packages are installed through the Store and are a different case.

Recommendation: SignPath Foundation if the owner decides to publish Qronos as open source. Otherwise no free,
publicly trusted option for the downloadable installer was found. The lowest-cost paid routes are Azure
Artifact Signing (if the owner or company is in an eligible country) and an OV certificate. MSIX through the
Store has no signing cost, but it is a separate packaging project (section 6).

## 2. SignPath Foundation eligibility matrix (Qronos at commit 731a872)

| # | SignPath Foundation condition (terms.html) | Qronos today | Evidence | Who closes it |
|---|---|---|---|---|
| B1 | OSI-approved licence for the project, with no commercial dual-licensing | **Not met.** No licence chosen | `README.en.md` "No licence has been chosen yet"; no root LICENSE file; GitHub `licenseInfo` null | Owner's decision (pending) |
| B2 | Public, verifiable source | **Not met.** The repository is private | `gh repo view`: `isPrivate: true` | Owner |
| B3 | Already released in the form that is signed, and actively maintained | **Not met.** No release | `gh release list`: empty | Owner |
| B4 | Verifiable build: GitHub workflow, artifact uploaded as a workflow artifact, all jobs on GitHub-hosted runners | **Not met.** Actions is blocked by billing; release builds run on the owner's machine with local caches | `.github/workflows/*` exist but cannot run; the r73 review-candidate was built on the owner's machine with a reused local Cargo target and locally cached prerequisites | Owner (billing), then the CI template here |
| B5 | No proprietary components except "System Libraries" (GPLv3 §1 definition) | **Open question.** The installer embeds the Microsoft VC++ 2015-2022 redistributable (its pin says redistribution needs a valid Visual Studio licence) and the WebView2 standalone installer. The runtime ships MSVCP140_1 / VCOMP140 / api-ms-win DLLs. `webview2-com-sys` is flagged for owner review | `build_support/vcredist_prerequisite.json`, `webview2_prerequisite.json`, the r73 notices inventory | Ask SignPath; owner review. Not assumed |
| B6 | Sign only binaries built from the project's own source; never upstream binaries | **Design constraint.** Only the installer is in `signOnly`. CPython DLLs, the PyInstaller bootloader, the NSIS stub, ffmpeg, whisper.cpp, crispasr and the Microsoft files stay unsigned by this certificate. Whether the frozen `qronos-runtime.exe` (PyInstaller bootloader plus Qronos code) counts as own code is for SignPath to rule | `signpath_release.prepare` (`signOnly`, `neverSignedWithThisCertificate`) | SignPath ruling |
| B7 | All components OSI-licensed | **Not met for runtime assets** (provisioned separately, never bundled). The openWakeWord pretrained models are CC BY-NC-SA 4.0 per upstream (non-commercial; not OSI). The Persian Chatterbox fine-tune's licence is not established. The qronos.onnx wake model is project-owned and not yet supplied | `build_support/runtime_assets.json` (`owner-review-required`, `project-owned`) | Owner review: relicence upstream permission, replace, or keep out of what is distributed |
| B8 | MFA for all members; Author, Reviewer and Approver roles; manual approval of every release | **Not verified here.** MFA settings and role assignments are not visible from this checkout; no SignPath project is configured in this repository | none in the repository | Owner |
| B9 | Code signing policy page with SignPath's attribution, team roles and a privacy statement | **Draft only** | `docs/signing/code-signing-policy.DRAFT.md` | Owner publishes after review |
| B10 | Product name and version metadata on signed binaries; uninstall facilities | Likely met (NSIS uninstaller; `tauri.conf.json` product name and version); confirm on the built file | `desktop/src-tauri/tauri.conf.json` | Check at the first real request |
| B11 | Approval and the certificate are SignPath's to give and revoke | External dependency, not software completion | — | SignPath Foundation |

`python -m build_support.signpath_release readiness` prints the gaps this checkout can show (B1, B7, the
empty pinned roots, the config) and lists the rest as not verifiable locally. It exits 1 while any is open.

## 3. Owner checklist (in order; nothing here has been done)

1. Decide whether Qronos becomes public open source. If yes, choose an OSI licence (owner's decision; ideally
   with legal review), add it at the repository root, and check that all project-owned code may be released
   under it.
2. Settle B7: for each `owner-review-required` runtime asset, record the licence that applies or replace the
   asset. Keep non-OSI models out of anything SignPath signs or distributes.
3. Ask SignPath about B5 and B6 (the Microsoft redistributables, the WebView2 installer, the frozen Python
   runtime) before applying. Record the answer.
4. Make the repository public and publish a first (unsigned) release in the form to be signed.
5. Restore GitHub Actions (billing) so the build runs on GitHub-hosted runners only.
6. Confirm MFA on every account involved (GitHub and SignPath). Assign the Author, Reviewer and Approver roles
   as the terms define them; whether one person may hold several roles is for SignPath to confirm.
7. Review and publish the code signing policy page and its privacy statement
   (`code-signing-policy.DRAFT.md`). The privacy wording describes personal-data handling, so a GDPR/privacy
   review is recommended before publishing.
8. Review and submit the application (`signpath-application.DRAFT.md`) on signpath.org.
9. After acceptance, in SignPath: create the project, a release signing policy with manual approval, and an
   artifact configuration. The CI template uploads a ZIP, so its root is `<zip-file>`, holding one
   `<pe-file>` for `qronos-desktop_*_x64-setup.exe`. Store the API token only as the repository secret
   `SIGNPATH_API_TOKEN`.
10. Pin the public root (section 4). In one reviewed change, commit:
    - `config/signpath_foundation_signer.cer`: the reviewed public signing certificate, DER;
    - `config/signpath_release.json`, written from the template with `"enabled": true` and
      `"authenticodeConfig": ".cache/signing/authenticode.json"`.
    On a machine that verifies by hand, write that Authenticode config (signtool, the certificate, its
    thumbprint, the time-stamp URL) and run `python -m build_support.signpath_release check --config ...`.
11. In the workflow template, set `TIMESTAMP_URL` to the time-stamp service recorded at review. The action
    commits are pinned as of 2026-10-14; move to newer releases only in a reviewed change that re-resolves them
    and updates `ACTION_PINS` in the tests. Copy the file into `.github/workflows/` in a reviewed change, and
    run it by hand.
12. The `verify` job and the release preflight must both pass on the returned file before anything is
    published. SmartScreen reputation then builds over time; it is not a gate this project controls.

## 4. Pinning the public root: process only, no root named

`config/authenticode_public_roots.json` stays empty in this change. No root is named here, because none has
been verified here. When SignPath has signed a first file:

1. Read the chain Windows builds for that file, offline and read-only (`authenticode_release.signature_chains`,
   or `signtool verify /pa /all /tw /v`). Do not add anything to any certificate store.
2. Confirm that the terminal root is a member of Microsoft's Trusted Root Program, from an independent primary
   source (Microsoft's published participant list or the CCADB). Compare its SHA-256 and SHA-1 by value.
3. Add one anchor entry (`subject`, `sha256`, `sha1`, and `provenance` with `source`, `reviewedBy`,
   `reviewedAt` and `evidence`) in a reviewed commit, separate from any other change.
4. Write the Authenticode config for SignPath Foundation's signing certificate: its public DER file, its SHA-1
   thumbprint, the SDK `signtool` path on the machine that verifies, and the time-stamp URL. Set
   `publisherSubject` in the SignPath config to the signer subject exactly as Windows reports it; its CN is
   SignPath Foundation.
5. When SignPath renews its certificate, repeat steps 1-4. A changed thumbprint or root is refused until it is
   reviewed.

## 5. What the gate checks, and what it does not

`prepare` writes nothing unless:

- the installer is the versioned `qronos-desktop_<version>_x64-setup.exe`, unsigned, and exactly the file in
  its build provenance;
- the provenance is attributable to the clean commit checked out now;
- the build is not a `packaging-check`.

It records the file SHA-256 and the SHA-256 of the bytes a signature will cover (the PE file without its
checksum field and certificate-table entry, zero-padded to 8 bytes). The record is never overwritten and is
marked `submitted: false`.

`verify` passes a returned file only if:

- the unsigned installer and the clean commit are still those in the request;
- the returned file's signed content equals the submitted content, so only a signature was added;
- the existing `authenticode_release.verify_signed` accepts it (`signtool verify /pa /all /tw`, WinVerifyTrust
  on every signature, every chain ending in a pinned reviewed root, the configured certificate as primary
  signer);
- the signer subject equals the configured SignPath Foundation subject.

The receipt never opens the release preflight; the preflight verifies the file again itself.

Not checked here:

- certificate revocation (offline);
- SignPath's own origin-verification and approval records;
- SmartScreen reputation.

The padding rule is proven on synthetic PE files only. If a real signer lays the file out differently, `verify`
refuses the file. It never accepts it.

Not covered by this candidate: the desktop exe and `qronos-runtime.exe` inside the installer stay unsigned.
Signing them needs a signing pass before NSIS packaging, after SignPath has ruled on B6.

In CI, the `verify` job writes the runner's Authenticode config to `.cache/signing/authenticode.json`. The
`.cache/` folder is git-ignored, so the checkout stays clean. The config holds the SDK `signtool` on that
runner, the committed reviewed certificate and that certificate's own SHA-1. The job then runs `check` and
`verify`. The workflow never publishes or creates a release.

## 6. Fallback: Microsoft Store with MSIX (checklist only; not implemented)

This route does not sign the current NSIS installer. It is a separate Store package that Microsoft signs
(https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options). Before starting:

1. A Microsoft Partner Center developer account and a reserved app name. Its terms and any fee are for the
   owner to confirm.
2. MSIX packaging. `build_desktop` makes NSIS only (`SUPPORTED_BUNDLES = ("nsis",)`), so a Store build needs a
   new packaging step. Its identity (name, publisher) must match Partner Center.
3. The embedded prerequisites. The NSIS hook installs WebView2 and the VC++ redistributable. A Store package
   cannot run those installers the same way, so their Store-compatible delivery must be designed and checked.
4. Updates. The Store updates Store installs. The Ed25519 local update-package path would need a reviewed
   decision for the Store build (kept separate, or turned off there).
5. The backend sidecar, the separately provisioned runtime assets (written under the user's local app-data
   folder), the per-user data folder and uninstall clean-up must be checked against MSIX file-system and
   process rules.
6. Store certification: the app's privacy statement and content declarations, and the same licence review
   (B5, B7) for everything the package contains.

No MSIX work is part of this change.
