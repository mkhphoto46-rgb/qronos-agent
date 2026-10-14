"""Opt-in SignPath Foundation readiness (build_support/signpath_release.py).

Safety of these fixtures: nothing is signed, submitted or downloaded, and signtool is NEVER run. The PE files
are small synthetic byte strings written to tmp_path; "signing" one appends a fake certificate table the way a
signer lays it out. Authenticode verification is the real authenticode_release.verify_signed with its native
seams faked (the runner, signtool check, certificate facts and chains), exactly as in
tests/test_authenticode_release.py. Anchor sets are tmp_path files; the shipped ones are only read.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from build_support import authenticode_release as authenticode
from build_support import desktop_installer_verify
from build_support import signpath_release as signpath
from build_support.signpath_release import SignPathError
from tests.test_authenticode_release import (ENTERPRISE_ROOT, LEAF, PUBLIC_ROOT, THUMB, FakeSigntool, anchor_entry,
                                             good_facts, product, write_anchors, write_config)

ROOT = Path(__file__).resolve().parent.parent
PUBLISHER = "CN=SignPath Foundation, O=fixture stand-in"
ORGANIZATION = "0f0e0d0c-0b0a-4908-8706-050403020100"


# ------------------------------------------------------------- synthetic PE files

def make_pe(body: bytes = b"x" * 1001, *, plus: bool = False, directories: int = 16) -> bytes:
    """A minimal PE image: DOS header, PE signature, COFF header, an optional header, then the body (overlay)."""
    header, optional_size = 0x80, (240 if plus else 224)
    data = bytearray(header + 24 + optional_size)
    data[:2] = b"MZ"
    data[60:64] = header.to_bytes(4, "little")
    data[header:header + 4] = b"PE\0\0"
    data[header + 20:header + 22] = optional_size.to_bytes(2, "little")
    start = header + 24
    data[start:start + 2] = (0x20B if plus else 0x10B).to_bytes(2, "little")
    count = start + (108 if plus else 92)
    data[count:count + 4] = directories.to_bytes(4, "little")
    return bytes(data) + body


def fake_sign(unsigned: bytes, *, plus: bool = False, pad: bytes = b"\0", tail: bytes = b"",
              certificate: bytes = b"\x30" * 24) -> bytes:
    """What a signer does: new checksum, pad to 8 bytes, append the certificate table, point entry 4 at it."""
    data = bytearray(unsigned)
    start = 0x80 + 24
    data[start + 64:start + 68] = (0x5A5A).to_bytes(4, "little")
    data += pad * (-len(data) % 8)
    table = len(data)
    entry = start + (112 if plus else 96) + 32
    data[entry:entry + 8] = table.to_bytes(4, "little") + len(certificate).to_bytes(4, "little")
    return bytes(data) + certificate + tail


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("plus", [False, True])
@pytest.mark.parametrize("length", [1001, 1000 - (0x80 + 24 + 224) % 8])  # unaligned, and already 8-aligned
def test_a_signed_file_covers_exactly_the_unsigned_bytes(tmp_path, plus, length):
    unsigned = make_pe(b"y" * length, plus=plus)
    a = write(tmp_path / "u.exe", unsigned)
    b = write(tmp_path / "s.exe", fake_sign(unsigned, plus=plus))
    assert signpath.payload_sha256(a, signed=False) == signpath.payload_sha256(b, signed=True)


def test_any_changed_byte_or_nonzero_padding_changes_the_signed_content(tmp_path):
    unsigned = make_pe()
    base = signpath.payload_sha256(write(tmp_path / "u.exe", unsigned), signed=False)
    changed = bytearray(unsigned)
    changed[-5] ^= 1
    assert signpath.payload_sha256(write(tmp_path / "c.exe", fake_sign(bytes(changed))), signed=True) != base
    assert signpath.payload_sha256(write(tmp_path / "p.exe", fake_sign(unsigned, pad=b"\1")), signed=True) != base


@pytest.mark.parametrize(("data", "signed", "message"), [
    (fake_sign(make_pe(), tail=b"appended"), True, "not at its end"),
    (make_pe(), True, "missing"),
    (fake_sign(make_pe()), False, "must be unsigned"),
    (b"not a PE file at all" * 4, False, "not a PE file"),
    (make_pe(directories=4), False, "no certificate-table entry"),
    (make_pe().replace(b"PE" + bytes(2), b"NE" + bytes(2), 1), False, "no PE signature"),
    (make_pe()[:0x90], False, "outside the file"),
])
def test_malformed_or_extended_files_are_refused(tmp_path, data, signed, message):
    with pytest.raises(SignPathError, match=message):
        signpath.payload_sha256(write(tmp_path / "f.exe", data), signed=signed)


# ------------------------------------------------------------------------ config

def config_dict(tmp_path: Path, **changes) -> dict:
    config = {"schema": 1, "enabled": True, "organizationId": ORGANIZATION, "projectSlug": "qronos",
              "signingPolicySlug": "release-signing", "artifactConfigurationSlug": "nsis-installer",
              "apiTokenSecretName": "SIGNPATH_API_TOKEN", "sourceRepository": "https://github.com/example/qronos",
              "publisherSubject": PUBLISHER, "authenticodeConfig": str(write_config(tmp_path))}
    config.update(changes)
    return config


def config_file(tmp_path: Path, **changes) -> Path:
    path = tmp_path / "signpath.json"
    path.write_text(json.dumps(config_dict(tmp_path, **changes)), encoding="utf-8")
    return path


def test_a_complete_config_loads_and_check_contacts_nothing(tmp_path):
    config = signpath.load_config(config_file(tmp_path))
    checked = signpath.check(config)
    assert checked["certificateThumbprintSha1"] == THUMB and checked["network"] == "none"
    assert checked["submitted"] is False


def test_the_shipped_template_is_refused_as_it_is_and_even_when_enabled(tmp_path):
    with pytest.raises(SignPathError, match="not enabled"):
        signpath.load_config(signpath.TEMPLATE)
    template = json.loads(signpath.TEMPLATE.read_text(encoding="utf-8"))
    assert set(template) == signpath.CONFIG_FIELDS and template["enabled"] is False
    template["enabled"] = True
    path = tmp_path / "enabled-template.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    with pytest.raises(SignPathError, match="placeholder"):
        signpath.load_config(path)


@pytest.mark.parametrize("changes", [
    {"enabled": "true"}, {"schema": 2}, {"extra": "x"},
    {"organizationId": "not-a-uuid"}, {"projectSlug": "a b"}, {"signingPolicySlug": "../x"},
    {"apiTokenSecretName": "signpath_api_token"}, {"apiTokenSecretName": "A" * 80},
    {"sourceRepository": "https://gitlab.com/example/qronos"}, {"sourceRepository": "http://github.com/a/b"},
    {"publisherSubject": "CN=Qronos"}, {"publisherSubject": "CN=SignPath Foundation Imitation"},
    {"publisherSubject": "O=SignPath Foundation"}, {"authenticodeConfig": "../outside/authenticode.json"},
    {"authenticodeConfig": ""},
])
def test_a_malformed_config_is_refused(tmp_path, changes):
    with pytest.raises(SignPathError):
        signpath.load_config(config_file(tmp_path, **changes))


@pytest.mark.skipif(os.name != "nt", reason="drive-relative and rooted paths are Windows path forms")
@pytest.mark.parametrize("named", ["C:relative.json", "\\rooted.json"])
def test_a_drive_relative_or_rooted_authenticode_config_is_refused(tmp_path, named):
    with pytest.raises(SignPathError, match="inside the repository"):
        signpath.load_config(config_file(tmp_path, authenticodeConfig=named))


def test_a_repository_relative_authenticode_config_is_read_inside_the_root(tmp_path):
    root = tmp_path / "root"
    (root / ".cache" / "signing").mkdir(parents=True)
    config = signpath.load_config(config_file(tmp_path, authenticodeConfig=".cache/signing/authenticode.json"))
    (root / ".cache" / "signing" / "authenticode.json").write_bytes((tmp_path / "authenticode.json").read_bytes())
    assert signpath.authenticode_config_path(config, root) == root.absolute() / ".cache" / "signing" / "authenticode.json"
    assert signpath.check(config, root)["certificateThumbprintSha1"] == THUMB


def test_a_config_missing_a_field_is_refused(tmp_path):
    config = config_dict(tmp_path)
    del config["publisherSubject"]
    path = tmp_path / "short.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SignPathError, match="exactly"):
        signpath.load_config(path)


# ------------------------------------------------------------- prepare / verify

@pytest.fixture
def build(tmp_path, monkeypatch):
    root = product(tmp_path / "root")
    unsigned = write(tmp_path / "out" / "qronos-desktop_0.2.0_x64-setup.exe", make_pe())
    provenance = tmp_path / "out" / "installer-provenance.json"
    provenance.write_text(json.dumps({
        "purpose": "review-candidate",
        "installer": {"fileName": unsigned.name, "bytes": unsigned.stat().st_size,
                      "sha256": hashlib.sha256(unsigned.read_bytes()).hexdigest(), "signed": False},
        "attributableToDesktopSource": True, "desktopSource": {"commit": "c" * 40, "clean": True}}),
        encoding="utf-8")
    state = {"commit": "c" * 40, "clean": True, "changedPaths": []}
    monkeypatch.setattr(desktop_installer_verify, "source_state", lambda path: dict(state))
    (tmp_path / "requests").mkdir()
    return {"root": root, "unsigned": unsigned, "provenance": provenance, "state": state,
            "config": signpath.load_config(config_file(tmp_path)), "out": tmp_path / "requests", "tmp": tmp_path}


def prepared(build) -> Path:
    return signpath.prepare(build["config"], build["unsigned"], build["provenance"], build["out"], root=build["root"])


def test_prepare_records_the_exact_unsigned_installer_and_submits_nothing(build):
    request = json.loads(prepared(build).read_text(encoding="utf-8"))
    unsigned = build["unsigned"]
    assert request["dryRun"] is True and request["submitted"] is False
    assert request["artifact"] == {"fileName": unsigned.name, "bytes": unsigned.stat().st_size,
                                   "sha256": hashlib.sha256(unsigned.read_bytes()).hexdigest(),
                                   "authenticodePayloadSha256": signpath.payload_sha256(unsigned, signed=False)}
    assert request["sourceCommit"] == "c" * 40 and request["signOnly"] == [unsigned.name]
    assert all(request[key] == build["config"][key] for key in signpath.BOUND_FIELDS)
    assert "token" not in json.dumps(request).lower()
    with pytest.raises(SignPathError, match="never replaced"):
        prepared(build)


def test_prepare_refuses_a_signed_or_unattributable_or_check_only_build(build):
    clean = build["unsigned"].read_bytes()
    build["unsigned"].write_bytes(fake_sign(clean))
    with pytest.raises(SignPathError):  # signed, and no longer the file in the provenance
        prepared(build)
    build["unsigned"].write_bytes(clean)
    build["state"]["clean"] = False
    with pytest.raises(SignPathError, match="clean commit"):
        prepared(build)
    build["state"]["clean"] = True
    record = json.loads(build["provenance"].read_text(encoding="utf-8"))
    build["provenance"].write_text(json.dumps({**record, "purpose": "packaging-check"}), encoding="utf-8")
    with pytest.raises(SignPathError, match="packaging-check"):
        prepared(build)
    assert not list(build["out"].iterdir())


def test_prepare_refuses_a_wrongly_named_installer_or_output_folder(build):
    renamed = write(build["tmp"] / "other" / "qronos-setup.exe", build["unsigned"].read_bytes())
    with pytest.raises(SignPathError, match="must be"):
        signpath.prepare(build["config"], renamed, build["provenance"], build["out"], root=build["root"])
    with pytest.raises(SignPathError, match="--out"):
        signpath.prepare(build["config"], build["unsigned"], build["provenance"], build["tmp"] / "missing",
                         root=build["root"])


def chains_for(root: dict, *, subject: str = PUBLISHER, signer: str = THUMB):
    return lambda path: [{"index": 0, "chain": [{**LEAF, "subject": subject, "sha1": signer}, dict(root)]}]


def options(tmp_path: Path, **changes) -> dict:
    found = {"runner": FakeSigntool(),
             "tool": lambda path: {"path": "C:\\SDK\\signtool.exe", "sha256": "0" * 64,
                                   "signer": "Microsoft Corporation"},
             "facts": lambda der: good_facts(subject=PUBLISHER), "chains": chains_for(PUBLIC_ROOT),
             "anchors": authenticode.load_anchors(write_anchors(tmp_path, [anchor_entry(PUBLIC_ROOT)], "pin.json"))}
    found.update(changes)
    return found


@pytest.fixture
def returned(build):
    request = prepared(build)
    signed = write(build["tmp"] / "signed" / build["unsigned"].name, fake_sign(build["unsigned"].read_bytes()))
    return build, request, signed


def verify(returned, **changes) -> Path:
    build, request, signed = returned
    return signpath.verify_returned(build["config"], request, build["unsigned"], signed, root=build["root"],
                                    **options(build["tmp"], **changes))


windows_only = pytest.mark.skipif(os.name != "nt", reason="the Authenticode verifier is Windows-only")


@windows_only
def test_a_returned_file_passes_only_through_the_existing_verifier_and_writes_a_receipt(returned):
    build, request, signed = returned
    fake = FakeSigntool()
    receipt = json.loads(verify(returned, runner=fake).read_text(encoding="utf-8"))
    assert [call[1:5] for call in fake.calls] == [["verify", "/pa", "/all", "/tw"]]
    assert receipt["signedSha256"] == hashlib.sha256(signed.read_bytes()).hexdigest() != receipt["unsignedSha256"]
    assert receipt["signer"] == {"subject": PUBLISHER, "thumbprintSha1": THUMB}
    assert [s["root"]["sha256"] for s in receipt["signatures"]] == [PUBLIC_ROOT["sha256"]]
    assert receipt["verify"]["by"] == "build_support.authenticode_release.verify_signed"
    assert "SmartScreen reputation (Microsoft builds it over time; never claimed)" in receipt["notVerifiedHere"]
    with pytest.raises(SignPathError, match="never replaced"):
        verify(returned)


@windows_only
@pytest.mark.parametrize(("changes", "message"), [
    ({"runner": FakeSigntool(verify_exit=2)}, "exit 2"),  # e.g. no time stamp (/tw)
    ({"runner": FakeSigntool(verify_exit=1)}, "did not succeed"),
    ({"chains": chains_for(ENTERPRISE_ROOT)}, "pinned public root"),
    ({"chains": chains_for(PUBLIC_ROOT, signer="EE" * 20)}, "configured certificate"),
    ({"chains": chains_for(PUBLIC_ROOT, subject="CN=SignPath Foundation, O=someone else")}, "SignPath Foundation subject"),
    ({"chains": lambda path: []}, "no signature"),
    ({"anchors": None}, "No reviewed public trust anchor"),  # the SHIPPED anchor set: empty, so all is refused
])
def test_an_unproven_signature_or_another_publisher_is_refused_and_writes_no_receipt(returned, changes, message):
    with pytest.raises(SignPathError, match=message):
        verify(returned, **changes)
    assert not os.path.lexists(returned[2].with_name(returned[2].name + ".signpath.json"))


@windows_only
def test_a_returned_file_with_other_content_never_reaches_the_verifier(returned):
    build, request, signed = returned
    changed = bytearray(build["unsigned"].read_bytes())
    changed[-1] ^= 1
    signed.write_bytes(fake_sign(bytes(changed)))
    fake = FakeSigntool()
    with pytest.raises(SignPathError, match="plus a signature"):
        verify(returned, runner=fake)
    assert fake.calls == []


@windows_only
def test_the_request_the_unsigned_file_and_the_source_must_still_match(returned):
    build, request, signed = returned
    with pytest.raises(SignPathError, match="separate file"):
        signpath.verify_returned(build["config"], request, build["unsigned"], build["unsigned"], root=build["root"],
                                 **options(build["tmp"]))
    other = {**build["config"], "projectSlug": "another-project"}
    with pytest.raises(SignPathError, match="projectSlug"):
        signpath.verify_returned(other, request, build["unsigned"], signed, root=build["root"],
                                 **options(build["tmp"]))
    build["state"]["commit"] = "d" * 40
    with pytest.raises(SignPathError, match="clean commit"):
        verify(returned)
    build["state"]["commit"] = "c" * 40
    build["unsigned"].write_bytes(make_pe(b"z" * 1001))
    with pytest.raises(SignPathError, match="not the one in the signing request"):
        verify(returned)


# -------------------------------------------------- readiness, template, boundaries

def test_readiness_reports_this_checkout_as_not_eligible_and_never_assumes_external_conditions():
    found = signpath.readiness(ROOT)
    gaps = " | ".join(found["localGaps"])
    assert found["ready"] is False and found["network"] == "none"
    assert "public roots" in gaps and "config: no SignPath config" in gaps
    if not any((ROOT / name).is_file() for name in signpath.LICENCE_FILES):
        assert "owner's pending decision" in gaps
    assert "runtime-asset licences" in gaps and "models/openwakeword/melspectrogram.onnx" in gaps
    assert any("accepted the application" in item for item in found["externalNotVerifiableHere"])
    with_template = signpath.readiness(ROOT, signpath.TEMPLATE)
    assert any(gap.startswith("config: SignPath signing is not enabled") for gap in with_template["localGaps"])


def test_the_cli_exits_one_while_not_ready_or_not_configured(capsys):
    assert signpath.main(["readiness"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert signpath.main(["check", "--config", str(signpath.TEMPLATE)]) == 1
    assert "not enabled" in json.loads(capsys.readouterr().out)["error"]


WORKFLOW = ROOT / "docs" / "signing" / "signpath-release.workflow.yml"
#: Reviewed 2026-10-14: each release tag resolved to its commit in the upstream repository (GitHub API, tags
#: dereferenced), and the input names read from action.yml at exactly that commit.
ACTION_PINS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1", {
        "repository", "ref", "token", "ssh-key", "ssh-known-hosts", "ssh-strict", "ssh-user", "persist-credentials",
        "path", "clean", "filter", "sparse-checkout", "sparse-checkout-cone-mode", "fetch-depth", "fetch-tags",
        "show-progress", "lfs", "submodules", "set-safe-directory", "github-server-url",
        "allow-unsafe-pr-checkout"}),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0", {
        "python-version", "python-version-file", "cache", "architecture", "check-latest", "token",
        "cache-dependency-path", "update-environment", "allow-prereleases", "freethreaded", "pip-version"}),
    "actions/setup-node": ("949feb2413d6458794dcd2491c4babbbce0c15c1", "v7.1.0", {
        "node-version", "node-version-file", "architecture", "check-latest", "registry-url", "scope", "token",
        "cache", "package-manager-cache", "cache-dependency-path", "mirror", "mirror-token"}),
    "actions/upload-artifact": ("cf430e030ddbb5b0abf93d22962f4752f3646cd9", "v7.0.2", {
        "name", "path", "if-no-files-found", "retention-days", "compression-level", "overwrite",
        "include-hidden-files", "archive"}),
    "actions/download-artifact": ("9000827ccba6bdab643e8b6fd33ac0654aef8333", "v8.0.2", {
        "name", "artifact-ids", "path", "pattern", "merge-multiple", "github-token", "repository", "run-id",
        "skip-decompress", "digest-mismatch"}),
    "actions/attest": ("1e69f48acb82d1966a394da916b4c1698aa569d6", "v4.2.2", {
        "subject-path", "subject-digest", "subject-name", "subject-checksums", "subject-version", "sbom-path",
        "predicate-type", "predicate", "predicate-path", "push-to-registry", "create-storage-record",
        "show-summary", "github-token"}),
    "signpath/github-action-submit-signing-request": ("f6d04783b4569d051e0c80105fe66e82819d0092", "v3.0", {
        "connector-url", "api-token", "organization-id", "project-slug", "signing-policy-slug",
        "artifact-configuration-slug", "github-artifact-id", "github-token", "parameters",
        "wait-for-completion-timeout-in-seconds", "service-unavailable-timeout-in-seconds",
        "download-signed-artifact-timeout-in-seconds", "wait-for-completion", "output-artifact-directory",
        "skip-decompress"}),
}


def workflow_steps(text: str) -> list[tuple[str, str, str, dict[str, str]]]:
    """(action, commit, comment, with-inputs) of every `uses:` step, read line by line (the template is plain YAML)."""
    lines = text.splitlines()
    steps = []
    for number, line in enumerate(lines):
        match = re.match(r"^\s*(?:- )?uses:\s*([^@\s]+)@(\S+)(?:\s+#\s*(.*))?$", line)
        if not match:
            continue
        column, inputs, in_with = line.index("uses:"), {}, False
        for following in lines[number + 1:]:
            if not following.strip():
                continue
            indent = len(following) - len(following.lstrip())
            if indent < column or following.lstrip().startswith("- "):
                break
            if indent == column:
                in_with = following.strip() == "with:"
            elif in_with and indent == column + 2:
                key, _, value = following.strip().partition(":")
                inputs[key] = value.strip()
        steps.append((match.group(1), match.group(2), match.group(3) or "", inputs))
    return steps


def test_the_workflow_is_a_template_github_never_runs():
    text = WORKFLOW.read_text(encoding="utf-8")
    workflows = ROOT / ".github" / "workflows"
    assert not any("signpath" in path.read_text(encoding="utf-8").lower() for path in workflows.glob("*.y*ml"))
    triggers = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert triggers.strip() == "workflow_dispatch:"
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == {"SIGNPATH_API_TOKEN"}
    assert json.loads(signpath.TEMPLATE.read_text(encoding="utf-8"))["apiTokenSecretName"] == "SIGNPATH_API_TOKEN"
    assert all(line.strip() == "runs-on: windows-latest" for line in text.splitlines() if "runs-on:" in line)
    assert "self-hosted" not in text


def test_every_action_is_pinned_to_its_reviewed_commit_with_only_its_real_inputs():
    steps = workflow_steps(WORKFLOW.read_text(encoding="utf-8"))
    assert {action for action, *_ in steps} == set(ACTION_PINS)
    for action, commit, comment, inputs in steps:
        expected_commit, tag, known_inputs = ACTION_PINS[action]
        assert commit == expected_commit and re.fullmatch(r"[0-9a-f]{40}", commit), action
        assert comment.startswith(tag), (action, comment)
        assert set(inputs) <= known_inputs, (action, set(inputs) - known_inputs)
    upload = [inputs for action, _, _, inputs in steps if action == "actions/upload-artifact"]
    assert all(inputs.get("if-no-files-found") == "error" for inputs in upload)
    submit = next(inputs for action, _, _, inputs in steps if action.startswith("signpath/"))
    assert {submit[key] for key in ("organization-id", "project-slug", "signing-policy-slug",
                                    "artifact-configuration-slug")} == {
        f"${{{{ steps.config.outputs.{key} }}}}" for key in ("organizationId", "projectSlug", "signingPolicySlug",
                                                              "artifactConfigurationSlug")}
    assert submit["wait-for-completion"] == "true" and submit["github-artifact-id"].startswith("${{ needs.build.")


def _declared_options(module: str) -> set[str]:
    tree = ast.parse((ROOT / "build_support" / f"{module}.py").read_text(encoding="utf-8"))
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("--")}


def test_every_builder_command_and_option_in_the_template_exists_in_the_current_source():
    from build_support import build_desktop, desktop_prerequisites
    text = WORKFLOW.read_text(encoding="utf-8")
    joined = re.sub(r"\\\n\s*", " ", text)
    commands = re.findall(r"python -m build_support\.(\w+)([^\n]*)", joined)
    assert {module for module, _ in commands} == {"build_runtime", "build_desktop", "signpath_release"}
    for module, rest in commands:
        options = set(re.findall(r"(?<!\S)(--[a-z][a-z0-9-]*)", rest))
        assert options <= _declared_options(module), (module, options - _declared_options(module))
        if module == "signpath_release":
            assert rest.split()[0] in {"readiness", "check", "prepare", "verify"}
    assert re.search(r"--purpose (\S+)", joined).group(1) in build_desktop.PURPOSES
    assert re.search(r"--purpose (\S+)", joined).group(1) != "packaging-check"  # prepare refuses those
    # the pinned prerequisite files, fetched into the folders build_desktop reads by default
    pins = dict(re.findall(r"(\w+):(\w+)", re.search(r"for pin in ([^;]+);", text).group(1)))
    assert pins == {"webview2": desktop_prerequisites.CACHE_DIR.name,
                    "vcredist": desktop_prerequisites.VCREDIST_CACHE_DIR.name}
    assert desktop_prerequisites.CACHE_DIR.parent.name == desktop_prerequisites.VCREDIST_CACHE_DIR.parent.name \
        == ".cache"
    for name in pins:
        pin = json.loads((ROOT / "build_support" / f"{name}_prerequisite.json").read_text(encoding="utf-8"))
        assert pin["url"].startswith("https://") and pin["fileName"]
    # Tauri's NSIS tools: the URLs and SHA-1 values come from the reviewed constants, laid out as the preflight checks
    for name in re.findall(r"\bpin (\w+)\)", text):
        assert hasattr(desktop_prerequisites, name), name
    assert set(re.findall(r"\bpin (\w+)\)", text)) == {"NSIS_URL", "NSIS_SHA1", "NSIS_TAURI_UTILS_URL",
                                                        "NSIS_TAURI_UTILS_SHA1"}
    assert desktop_prerequisites.NSIS_URL.endswith("/nsis-3.11.zip") and '"$L/tauri/nsis-3.11" "$L/tauri/NSIS"' in text
    assert f'"$L/tauri/NSIS/{desktop_prerequisites.NSIS_TAURI_UTILS}"' in text
    assert "cargo fetch --locked" in text and "npm ci" in text
    # the request travels with the unsigned installer, under the name prepare gives it
    assert "qronos-desktop_*_x64-setup.exe.signpath-request.json" in text
    assert 'f"{path.name}.signpath-request.json"' in Path(signpath.__file__).read_text(encoding="utf-8")


def test_the_template_fails_closed_and_leaves_only_owner_values_open():
    text = WORKFLOW.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert [line.strip() for line in code if "<" in line] == [
        'TIMESTAMP_URL: "<owner: the RFC 3161 time-stamp URL recorded at review>"']
    # no SignPath config is committed today, so the first build step stops the run
    assert not (ROOT / "config" / "signpath_release.json").exists()
    assert "s.load_config(Path(sys.argv[1]).resolve())\" config/signpath_release.json" in text
    # the runner's Authenticode config lives in an ignored folder, so the checkout stays clean for verify
    assert "Path('.cache/signing/authenticode.json')" in text and "config/signpath_foundation_signer.cer" in text
    ignored = subprocess.run(["git", "check-ignore", "-q", ".cache/signing/authenticode.json"], cwd=ROOT)
    assert ignored.returncode == 0
    from build_support import authenticode_release
    assert callable(authenticode_release.program_files_x86)


def test_the_tool_makes_no_network_call_reads_no_environment_and_adds_no_trust():
    source = Path(signpath.__file__).read_text(encoding="utf-8")
    imported = {(node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
                for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names}
    assert {name.split(".")[0] for name in imported} == {"__future__", "argparse", "hashlib", "json", "os", "re",
                                                         "sys", "pathlib", "typing", "build_support", "core"}
    for forbidden in ("os.environ", "getenv", "shell=True", '"w"', "write_text", "write_bytes"):
        assert forbidden not in source, forbidden
    assert json.loads((ROOT / "config" / "authenticode_public_roots.json").read_text(encoding="utf-8"))["anchors"] == []
