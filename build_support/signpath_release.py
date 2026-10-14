"""Opt-in SignPath Foundation signing of a built Qronos installer: prepare, then verify what comes back.

Build tooling only (the owner's machine or a CI runner); nothing here is part of
the product, and nothing here makes a network call. It neither submits a signing
request nor signs anything: SignPath's own GitHub action does the submission
(docs/signing/signpath-release.workflow.yml, a template that is not enabled),
and a person in SignPath approves every release. Contract and owner steps:
docs/signing/free-code-signing.md.

Commands (``python -m build_support.signpath_release ...``):

  readiness [--config JSON]
      Lists the eligibility gaps this checkout can show (licence, runtime-asset
      licences, pinned public roots, the config). Exit 1 while any is open. The
      external conditions (public repository, a release, MFA, roles, SignPath's
      approval) are listed as unverifiable here, never as met.
  check   --config JSON
      Validates the SignPath config and the Authenticode config it names.
  prepare --config JSON --artifact EXE --build-provenance JSON --out DIR
      Writes DIR/EXE.signpath-request.json: the exact unsigned installer (file
      SHA-256 and the SHA-256 of the bytes its signature will cover), the clean
      source commit it was built from and the build provenance. Dry run: it is
      what a submission must match; it is not a submission.
  verify  --config JSON --request JSON --unsigned EXE --signed EXE
      Checks a file SignPath returned: it is the submitted installer plus a
      signature and nothing else, then the EXISTING Authenticode verifier
      (build_support/authenticode_release.verify_signed: every signature valid
      and time-stamped, every chain ending in a reviewed pinned public root, the
      configured certificate as primary signer) must accept it, and the signer
      must be the configured SignPath Foundation subject. Writes
      SIGNED.signpath.json beside it (never over an existing one).

The config is one JSON file holding no secret (the API token stays in the CI
secret store; only its NAME is recorded). config/signpath_release.template.json
is the shape; its placeholders and "enabled": false are refused, so the
template can never be used as it is:

  {"schema": 1, "enabled": true,
   "organizationId": "<SignPath organization id (UUID)>",
   "projectSlug": "...", "signingPolicySlug": "...", "artifactConfigurationSlug": "...",
   "apiTokenSecretName": "SIGNPATH_API_TOKEN",
   "sourceRepository": "https://github.com/<owner>/<repository>",
   "publisherSubject": "<the signer subject exactly as Windows reports it; CN=SignPath Foundation>",
   "authenticodeConfig": "<the authenticode_release config: an absolute path, or one inside the repository>"}

SignPath Foundation certificates are issued to SignPath Foundation, so the
publisher Windows shows is SignPath Foundation, not Qronos. Nothing here adds
a trusted root: config/authenticode_public_roots.json stays as reviewed (it
ships empty, so today every returned file is refused). The Ed25519 update key
(build_support/release_signing.py) is separate and untouched, and the release
preflight still opens only on authenticode_release.verify_signed; a receipt
written here never opens it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_support import authenticode_release as authenticode  # noqa: E402
from build_support import desktop_installer_verify, release_signing  # noqa: E402
from build_support.authenticode_release import AuthenticodeError  # noqa: E402
from core import local_update  # noqa: E402

CONFIG_FIELDS = {"schema", "enabled", "organizationId", "projectSlug", "signingPolicySlug",
                 "artifactConfigurationSlug", "apiTokenSecretName", "sourceRepository", "publisherSubject",
                 "authenticodeConfig"}
#: The fields a request records and a returned file is checked against.
BOUND_FIELDS = ("organizationId", "projectSlug", "signingPolicySlug", "artifactConfigurationSlug",
                "sourceRepository", "publisherSubject")
FOUNDATION_PUBLISHER = "SignPath Foundation"  # https://signpath.org/terms.html: the certificate is issued to it
TEMPLATE = ROOT / "config" / "signpath_release.template.json"
LICENCE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md")
#: Eligibility conditions no file in this checkout can show; they are never reported as met.
EXTERNAL_CONDITIONS = (
    "the source repository is public and the licence is OSI-approved, with no commercial dual-licensing",
    "the project is already released in the form that is signed, and actively maintained",
    "every team member uses multi-factor authentication for SignPath and the repository",
    "Author, Reviewer and Approver roles are assigned, and every release is approved manually",
    "the build runs in GitHub Actions on GitHub-hosted runners only (Actions is billing-blocked today)",
    "SignPath rules on the bundled Microsoft redistributables and the frozen Python runtime",
    "the code signing policy page and the privacy statement are published",
    "SignPath Foundation has accepted the application",
)

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_REPOSITORY = re.compile(r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_FOUNDATION_CN = re.compile(r"(?:^|,\s*)CN=" + re.escape(FOUNDATION_PUBLISHER) + r"\s*(?:,|$)")
_HASH_BLOCK = 1 << 20


class SignPathError(Exception):
    """A SignPath preparation or verification step was refused; the message says why."""


# ---------------------------------------------------------------------- config


def load_config(path: Path) -> dict[str, Any]:
    config_path = _plain_file(Path(path), "SignPath config")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SignPathError("The SignPath config is not JSON.") from error
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schema"] != 1:
        raise SignPathError(f"The SignPath config needs exactly {sorted(CONFIG_FIELDS)}.")
    if config["enabled"] is not True:
        raise SignPathError("SignPath signing is not enabled in this config; nothing is prepared or verified.")
    if not all(isinstance(config[key], str) for key in CONFIG_FIELDS - {"schema", "enabled"}):
        raise SignPathError("Every SignPath config value except schema and enabled is a string.")
    if any("<" in config[key] or ">" in config[key] for key in CONFIG_FIELDS - {"schema", "enabled"}):
        raise SignPathError("The SignPath config still holds a template placeholder.")
    if not _UUID.fullmatch(config["organizationId"]):
        raise SignPathError("organizationId must be the SignPath organization UUID.")
    for key in ("projectSlug", "signingPolicySlug", "artifactConfigurationSlug"):
        if not _SLUG.fullmatch(config[key]):
            raise SignPathError(f"{key} must be a plain SignPath slug.")
    if not _SECRET_NAME.fullmatch(config["apiTokenSecretName"]):
        raise SignPathError("apiTokenSecretName is the NAME of a CI secret (A-Z, 0-9, _), never a token.")
    if not _REPOSITORY.fullmatch(config["sourceRepository"]):
        raise SignPathError("sourceRepository must be https://github.com/<owner>/<repository>.")
    if not _FOUNDATION_CN.search(config["publisherSubject"]):
        raise SignPathError(f"publisherSubject must name CN={FOUNDATION_PUBLISHER}: SignPath Foundation "
                            "certificates are issued to it, never to the project.")
    named = Path(config["authenticodeConfig"])
    if not named.is_absolute() and (named.drive or named.root or ".." in named.parts or not named.parts):
        raise SignPathError("authenticodeConfig must be an absolute path or a path inside the repository.")
    return config


def authenticode_config_path(config: dict[str, Any], root: Path = ROOT) -> Path:
    """The Authenticode config the SignPath config names; a relative name is inside the repository (on a CI
    runner: an ignored folder such as .cache/signing, so the checkout stays clean)."""
    named = Path(config["authenticodeConfig"])
    return named if named.is_absolute() else Path(root).absolute() / named


def _plain_file(path: Path, what: str) -> Path:
    try:
        return authenticode._plain_file(path, what)
    except AuthenticodeError as error:
        raise SignPathError(str(error)) from error


def check(config: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    signing = authenticode.load_config(authenticode_config_path(config, root))
    return {"organizationId": config["organizationId"], "projectSlug": config["projectSlug"],
            "signingPolicySlug": config["signingPolicySlug"],
            "artifactConfigurationSlug": config["artifactConfigurationSlug"],
            "publisherSubject": config["publisherSubject"], "certificateThumbprintSha1": signing["thumbprintSha1"],
            "network": "none", "submitted": False}


# ------------------------------------------------------- the bytes a signature covers


def _pe_layout(handle: Any, size: int) -> tuple[int, int, int, int]:
    """(checksum offset, certificate-table directory entry offset, table offset, table size) of a PE file."""
    dos = handle.read(64)
    if len(dos) < 64 or dos[:2] != b"MZ":
        raise SignPathError("The installer is not a PE file.")
    header = int.from_bytes(dos[60:64], "little")
    if header + 24 > size:
        raise SignPathError("The installer's PE header is outside the file.")
    handle.seek(header)
    coff = handle.read(24)
    if coff[:4] != b"PE\0\0":
        raise SignPathError("The installer has no PE signature.")
    optional_size = int.from_bytes(coff[20:22], "little")
    optional = handle.read(optional_size)
    directories = {0x10B: 96, 0x20B: 112}.get(int.from_bytes(optional[:2], "little")) if len(optional) >= 2 else None
    if directories is None or len(optional) != optional_size or optional_size < directories + 40 \
            or int.from_bytes(optional[directories - 4:directories], "little") < 5:
        raise SignPathError("The installer's optional header has no certificate-table entry.")
    start = header + 24
    entry = directories + 32  # data directory 4: the attribute certificate table
    return (start + 64, start + entry, int.from_bytes(optional[entry:entry + 4], "little"),
            int.from_bytes(optional[entry + 4:entry + 8], "little"))


def payload_sha256(path: Path, *, signed: bool) -> str:
    """SHA-256 of the bytes an Authenticode signature covers: the file without its checksum field, its
    certificate-table entry and the table itself. An unsigned file is hashed as a signer lays it out, padded
    with zeros to an 8-byte boundary; equal values mean the signed file is the unsigned one plus a signature."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        checksum, entry, table, table_size = _pe_layout(handle, size)
        if signed:
            if table_size == 0 or table % 8 or table < entry + 8 or table + table_size != size:
                raise SignPathError("The signed file's certificate table is missing, misplaced or not at its end.")
            end, padding = table, 0
        else:
            if table or table_size:
                raise SignPathError("The installer submitted for signing must be unsigned.")
            end, padding = size, -size % 8
        digest = hashlib.sha256()
        for first, last in ((0, checksum), (checksum + 4, entry), (entry + 8, end)):
            handle.seek(first)
            remaining = last - first
            while remaining > 0:
                block = handle.read(min(_HASH_BLOCK, remaining))
                if not block:
                    raise SignPathError("The installer ended early.")
                digest.update(block)
                remaining -= len(block)
        digest.update(b"\0" * padding)
        return digest.hexdigest()


# ---------------------------------------------------------------- prepare / verify


def prepare(config: dict[str, Any], artefact: Path, build_provenance: Path, out: Path, *,
            root: Path = ROOT) -> Path:
    """Record exactly what may be submitted: this unsigned installer, from this clean commit. Submits nothing."""
    path = _artefact(artefact, root)
    out = Path(out)
    if not out.is_absolute() or not out.is_dir() or local_update._is_link(out):
        raise SignPathError("--out must be an existing plain folder given as an absolute path.")
    request_path = out / f"{path.name}.signpath-request.json"
    if os.path.lexists(request_path):
        raise SignPathError(f"{request_path.name} already exists; a request is never replaced.")
    unsigned = desktop_installer_verify.sha256_file(path)
    payload = payload_sha256(path, signed=False)
    try:
        build = authenticode._build_provenance(build_provenance, path, unsigned, root)
    except AuthenticodeError as error:
        raise SignPathError(str(error)) from error
    purpose = json.loads(Path(build["path"]).read_text(encoding="utf-8")).get("purpose")
    if purpose == "packaging-check":
        raise SignPathError("A packaging-check build is never submitted for signing.")
    if desktop_installer_verify.sha256_file(path) != unsigned:
        raise SignPathError("The installer changed while the request was prepared.")
    request = {"schema": 1, "provider": "SignPath Foundation (https://signpath.org/)", "dryRun": True,
               "submitted": False, **{key: config[key] for key in BOUND_FIELDS},
               "sourceCommit": build["sourceCommit"], "buildPurpose": purpose,
               "buildProvenance": {"fileName": Path(build["path"]).name, "sha256": build["sha256"]},
               "artifact": {"fileName": path.name, "bytes": path.stat().st_size, "sha256": unsigned,
                            "authenticodePayloadSha256": payload},
               "signOnly": [path.name],
               "neverSignedWithThisCertificate": "upstream binaries inside the installer (Python, PyInstaller, "
                                                 "NSIS, Microsoft redistributables, ffmpeg, whisper.cpp, crispasr)",
               "approval": "manual, by a SignPath Approver of this project (external; not done here)"}
    with open(request_path, "x", encoding="utf-8", newline="\n") as handle:  # never replaces a request
        handle.write(json.dumps(request, indent=2, sort_keys=True) + "\n")
    return request_path


def verify_returned(config: dict[str, Any], request_path: Path, unsigned: Path, signed: Path, *,
                    root: Path = ROOT, **verify_options: Any) -> Path:
    """A returned file passes only as the submitted installer plus a signature that the existing verifier
    accepts, by the configured SignPath Foundation signer. Writes SIGNED.signpath.json; opens no gate."""
    request_file = _plain_file(Path(request_path), "signing request")
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SignPathError("The signing request is not JSON.") from error
    if not isinstance(request, dict) or request.get("schema") != 1 or request.get("submitted") is not False \
            or not isinstance(request.get("artifact"), dict):
        raise SignPathError("The signing request is not one prepared by this tool.")
    for key in BOUND_FIELDS:
        if request.get(key) != config[key]:
            raise SignPathError(f"The signing request's {key} is not the configured one.")
    original, returned = _artefact(unsigned, root), _artefact(signed, root)
    if os.path.normcase(os.path.realpath(original)) == os.path.normcase(os.path.realpath(returned)):
        raise SignPathError("The returned file must be a separate file from the unsigned installer.")
    artifact = request["artifact"]
    if desktop_installer_verify.sha256_file(original) != artifact.get("sha256") \
            or payload_sha256(original, signed=False) != artifact.get("authenticodePayloadSha256"):
        raise SignPathError("The unsigned installer is not the one in the signing request.")
    now = desktop_installer_verify.source_state(root)
    if not now["clean"] or now["commit"] != request.get("sourceCommit"):
        raise SignPathError("The checked-out source is not the clean commit the request was prepared from.")
    before = desktop_installer_verify.sha256_file(returned)
    if payload_sha256(returned, signed=True) != artifact["authenticodePayloadSha256"]:
        raise SignPathError("The returned file is not the submitted installer plus a signature: its signed "
                            "content differs.")
    record = returned.with_name(returned.name + ".signpath.json")
    if os.path.lexists(record):
        raise SignPathError(f"{record.name} already exists; a receipt is never replaced.")
    try:
        verified = authenticode.verify_signed(authenticode.load_config(authenticode_config_path(config, root)),
                                              returned, root=root, **verify_options)
    except AuthenticodeError as error:
        raise SignPathError(f"The existing Authenticode verifier refused the returned file: {error}") from error
    if verified["sha256"] != before:
        raise SignPathError("The returned file changed while it was being verified.")
    if verified["signer"].get("subject") != config["publisherSubject"]:
        raise SignPathError("The returned file is not signed by the configured SignPath Foundation subject.")
    receipt = {"schema": 1, "provider": request["provider"],
               "request": {"fileName": request_file.name,
                           "sha256": desktop_installer_verify.sha256_file(request_file)},
               "sourceCommit": request["sourceCommit"], "unsignedSha256": artifact["sha256"],
               "signedSha256": before, "authenticodePayloadSha256": artifact["authenticodePayloadSha256"],
               "signer": verified["signer"], "signatures": verified["signatures"],
               "certificate": verified["certificate"],
               "verify": {"by": "build_support.authenticode_release.verify_signed",
                          "command": "signtool verify /pa /all /tw /v", "exit": verified["verifyExit"],
                          "outputSha256": verified["verifyOutputSha256"], "winVerifyTrust": "valid (offline)"},
               "notVerifiedHere": ["certificate revocation (offline build check)",
                                   "SignPath's own origin-verification and approval records (kept by SignPath)",
                                   "SmartScreen reputation (Microsoft builds it over time; never claimed)"]}
    with open(record, "x", encoding="utf-8", newline="\n") as handle:  # never replaces a receipt
        handle.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return record


def _artefact(path: Path, root: Path) -> Path:
    try:
        return authenticode._artefact(path, root)
    except AuthenticodeError as error:
        raise SignPathError(str(error)) from error


# -------------------------------------------------------------------- readiness


def readiness(root: Path = ROOT, config_path: Path | None = None) -> dict[str, Any]:
    """The eligibility gaps this checkout can show. External conditions are listed, never assumed met."""
    gaps: list[str] = []
    if not any((root / name).is_file() for name in LICENCE_FILES):
        gaps.append("licence: no licence file at the repository root; choosing an OSI licence is the owner's "
                    "pending decision (nothing is relicensed by this tool)")
    assets = json.loads((root / "build_support" / "runtime_assets.json").read_text(encoding="utf-8"))["assets"]
    unsettled = sorted({asset["path"] for asset in assets
                        if str(asset.get("licence", "")).startswith(("owner-review-required", "project-owned"))})
    if unsettled:
        gaps.append(f"runtime-asset licences: {len(unsettled)} provisioned assets are owner-review-required or "
                    f"project-owned without a stated licence: {', '.join(unsettled)}")
    anchors = json.loads((root / "config" / "authenticode_public_roots.json").read_text(encoding="utf-8"))
    if not anchors.get("anchors"):
        gaps.append("public roots: config/authenticode_public_roots.json pins no reviewed root, so every "
                    "returned file is refused until the SignPath Foundation chain's root is reviewed and pinned")
    if config_path is None:
        gaps.append("config: no SignPath config given (config/signpath_release.template.json is the shape)")
    else:
        try:
            load_config(config_path)
        except SignPathError as error:
            gaps.append(f"config: {error}")
    return {"ready": not gaps, "localGaps": gaps, "externalNotVerifiableHere": list(EXTERNAL_CONDITIONS),
            "network": "none"}


# ------------------------------------------------------------------------ CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("readiness").add_argument("--config", type=Path, default=None)
    commands.add_parser("check").add_argument("--config", required=True, type=Path)
    command = commands.add_parser("prepare")
    for name in ("--config", "--artifact", "--build-provenance", "--out"):
        command.add_argument(name, required=True, type=Path)
    command = commands.add_parser("verify")
    for name in ("--config", "--request", "--unsigned", "--signed"):
        command.add_argument(name, required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "readiness":
            found = readiness(config_path=arguments.config)
            result: dict[str, Any] = {"ok": found["ready"], **found}
        else:
            config = load_config(arguments.config)
            if arguments.command == "check":
                result = {"ok": True, "checked": check(config)}
            elif arguments.command == "prepare":
                result = {"ok": True, "request": str(prepare(config, arguments.artifact, arguments.build_provenance,
                                                             arguments.out))}
            else:
                result = {"ok": True, "receipt": str(verify_returned(config, arguments.request, arguments.unsigned,
                                                                     arguments.signed))}
    except (SignPathError, AuthenticodeError, release_signing.SigningError, local_update.UpdateError, OSError,
            ValueError, KeyError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
