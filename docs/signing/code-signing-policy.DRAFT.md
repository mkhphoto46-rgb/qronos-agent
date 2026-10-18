# DRAFT - Qronos code signing policy (not published)

> Draft for the owner's review. It applies only if SignPath Foundation accepts Qronos
> (docs/signing/free-code-signing.md). Every `[OWNER]` item must be filled in or confirmed before publishing.
> The privacy section describes personal-data handling: a GDPR/privacy review is recommended before it goes
> public. This draft is not legal advice.

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by
[SignPath Foundation](https://signpath.org).

## What is signed

- Only the Qronos Windows installer (`qronos-desktop_<version>_x64-setup.exe`), built by the project's
  GitHub Actions workflow on GitHub-hosted runners from the source at https://github.com/mkhphoto46-rgb/qronos-agent (currently PRIVATE;
  it must be public before signing).
- Upstream programs inside the installer keep their own publishers' signatures or stay unsigned. They are never
  signed with this certificate: the Python runtime, PyInstaller, NSIS, the Microsoft Visual C++ redistributable,
  the Microsoft WebView2 installer, ffmpeg, whisper.cpp and crispasr.
- Downloaded runtime assets (speech and voice models) are provisioned separately, checked against pinned
  SHA-256 values, and are not signed with this certificate.

## Team roles

PROPOSED, not yet configured in SignPath. The accounts are the repository's GitHub collaborators as observed on
2026-10-16; being a collaborator is not a SignPath role.

- Authors (may change the source): `amyn-asd`, `mkhphoto46-rgb` (proposed)
- Reviewers (review every change from someone outside the team): `amyn-asd`, `mkhphoto46-rgb` (proposed)
- Approvers (approve each signing request in SignPath): `mkhphoto46-rgb` (proposed, pending the owner's acceptance)

`[OWNER: confirm before publishing]` Every team member uses multi-factor authentication for GitHub and
SignPath. MFA is currently **UNCONFIRMED** for both accounts. Every release is approved by hand.

## Privacy

`[OWNER TO CONFIRM against the shipped features before publishing]`

This program will not transfer any information to other networked systems unless specifically requested by the
user or the person installing or operating it.

Qronos Core runs on the user's PC without an internet connection. Features that send data off the PC run only
when the user turns them on or asks for them, and can be turned off: `[OWNER: list each such feature and
what it sends]`. Updates are installed only from packages the
user provides and that carry the owner's update signature.

## Verifying a download

Windows shows **SignPath Foundation** as the publisher of a signed Qronos installer. Each release also lists the
installer's SHA-256 `[OWNER: release page]`. A GitHub artifact attestation can be checked with
`gh attestation verify <file> -R mkhphoto46-rgb/qronos-agent`; that is build provenance, not a Windows trust decision.
