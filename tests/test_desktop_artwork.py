"""Third-party template artwork (build_support/desktop_artwork.json): excluded from Qronos's Apache-2.0 scope and
attributed in the desktop notices with its pinned upstream licence texts. Fixtures write only to tmp_path; the
repository's registry, artwork and texts are only read. The installer side is in test_desktop_installer.py."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from build_support import desktop_notices as notices

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = json.loads((ROOT / "build_support" / "desktop_artwork.json").read_text(encoding="utf-8"))


def tracked(folder: str) -> set[str]:
    out = subprocess.run(["git", "ls-files", "--", folder], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return set(out.split())


def test_every_tracked_artwork_file_is_registered_with_its_committed_bytes_and_a_pinned_upstream():
    artwork = notices.load_artwork()
    listed = {entry["path"]: entry for entry in artwork.registry["files"]}
    assert set(listed) == set().union(*(tracked(folder) for folder in REGISTRY["folders"]))
    assert len(listed) == 19 and {"desktop/src-tauri/icons/icon.ico", "desktop/public/vite.svg",
                                  "desktop/public/tauri.svg", "desktop/src/assets/react.svg"} <= set(listed)
    for path, entry in listed.items():
        committed = subprocess.run(["git", "cat-file", "blob", f"HEAD:{path}"], cwd=ROOT, capture_output=True).stdout \
            or (ROOT / path).read_bytes()  # (before the first commit of a new file, the working tree)
        assert hashlib.sha256(committed).hexdigest() == entry["sha256"], path
    sources = {source["id"]: source for source in REGISTRY["sources"]}
    assert sources["create-tauri-app"]["revision"] == "5813af8d642c061fc049742895b21b44c24654e6"
    assert sources["create-vite"]["revision"] == "f3d31499c714fe5c5acf8355520624c662f9d79f"
    assert [text["gitBlob"] for text in sources["create-tauri-app"]["texts"]] == [
        "f433b1a53f5b830a205fd2df78e2b34974656c7b", "7e54d8e4c28e943a545c1bef79f8dfdb2a2affe2",
        "44b69b9f496badfd1242436aa58898e5b097bb6b"]  # LICENSE_APACHE-2.0, LICENSE_MIT, LICENSE.spdx upstream blobs
    assert sources["create-vite"]["texts"][0]["gitBlob"] == "063dad83542504679fc1db778e0788db5ffaabd3"
    attributes = subprocess.run(["git", "check-attr", "text", "--", "desktop/public/tauri.svg",
                                 "desktop/src-tauri/icons/icon.ico", "build_support/desktop_artwork/texts/x"],
                                cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    assert all(line.endswith("text: unset") for line in attributes)  # checkouts keep the pinned bytes


def copy_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The registry folders, the vendored texts and the registry, copied into a tmp root."""
    root = tmp_path / "root"
    for folder in REGISTRY["folders"]:
        shutil.copytree(ROOT / folder, root / folder)
    shutil.copytree(ROOT / "build_support" / "desktop_artwork", root / "build_support" / "desktop_artwork")
    registry = root / "build_support" / "desktop_artwork.json"
    registry.write_bytes((ROOT / "build_support" / "desktop_artwork.json").read_bytes())
    return root.resolve(), registry, (root / "build_support" / "desktop_artwork" / "texts").resolve()


@pytest.mark.parametrize(("damage", "message"), [
    (lambda root: (root / "desktop" / "public" / "logo.svg").write_text("<svg/>"), "differ from"),
    (lambda root: (root / "desktop" / "src" / "assets" / "react.svg").write_bytes(b"<svg>changed</svg>"),
     "not the listed upstream file"),
    (lambda root: (root / "desktop" / "src-tauri" / "icons" / "icon.ico").unlink(), "missing"),
    (lambda root: next((root / "build_support" / "desktop_artwork" / "texts").rglob("LICENSE_MIT")).write_bytes(b"x"),
     "not the pinned upstream file"),
    (lambda root: (root / "build_support" / "desktop_artwork" / "texts" / "EXTRA").write_bytes(b"x"),
     "differ from the registry"),
])
def test_unlisted_changed_missing_or_extra_artwork_or_licence_texts_are_refused(tmp_path, damage, message):
    root, registry, texts = copy_tree(tmp_path)
    notices.load_artwork(registry, root, texts)  # the faithful copy passes
    damage(root)
    with pytest.raises(notices.NoticeError, match=message):
        notices.load_artwork(registry, root, texts)


def test_the_desktop_notices_attribute_every_artwork_file_and_ship_its_upstream_licence_texts(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "LICENSE").write_text("Permission is hereby granted, free of charge, to any person.\n"
                                     "THE SOFTWARE IS PROVIDED AS IS.\n", encoding="utf-8")
    component = notices.Component("rust", "demo", "1.0.0", "MIT", None,
                                  "registry+https://github.com/rust-lang/crates.io-index", "sum", package.resolve())
    files = notices.check_coverage([component], {})
    out = tmp_path / "notices"
    artwork = notices.load_artwork()
    inventory = notices.write_notices([component], files, {}, out, notices.WINDOWS_TARGET, (), artwork=artwork)
    manifest = {entry["path"]: entry["sha256"] for entry in notices.verify_notice_output(out)["files"]}
    for source in REGISTRY["sources"]:
        for text in source["texts"]:
            path = f"artwork/{text['file']}"
            assert manifest[path] == text["sha256"]
            assert (out / path).read_bytes() == (ROOT / "build_support" / "desktop_artwork" / "texts" / text["file"]).read_bytes()
    assert [entry["path"] for entry in inventory["artwork"]["files"]] == [e["path"] for e in REGISTRY["files"]]
    assert "NOT covered by Qronos's Apache-2.0 licence" in inventory["artwork"]["note"]
    body = (out / notices.NOTICE_TEXT).read_text(encoding="utf-8")
    section = body.split("Third-party artwork in the desktop application", 1)[1]
    for entry in REGISTRY["files"]:
        assert f"{entry['path']} ({entry['bytes']} bytes, SHA-256 {entry['sha256']})" in section
    assert "no trademark right is granted" in section and "https://github.com/tauri-apps/create-tauri-app at " in section


def test_notice_and_readmes_exclude_the_template_artwork_from_the_apache_scope():
    notice = " ".join((ROOT / "NOTICE").read_text(encoding="utf-8").split())
    for path in ("desktop/src-tauri/icons/", "desktop/public/tauri.svg", "desktop/public/vite.svg",
                 "desktop/src/assets/react.svg", "build_support/desktop_artwork.json", "create-tauri-app",
                 "create-vite", "no trademark right is granted", "They are not Qronos's own work."):
        assert path in notice, path
    for readme in ("README.en.md", "README.md"):
        text = (ROOT / readme).read_text(encoding="utf-8")
        assert "build_support/desktop_artwork.json" in text and "desktop/src-tauri/icons/" in text, readme
