"""sdk_setup.install accepts an installation only when the installed source.properties revisions equal the pins.

Fixture only: the SDK root is a tmp_path folder and sdkmanager is a fake that writes source.properties. No
download, no real sdkmanager, no licence. This proves revision agreement, not byte identity of installed files."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdk_setup  # noqa: E402

PINNED = {"platforms;android-36": "2", "build-tools;36.0.0": "36.0.0"}


def fake_install(tmp_path, monkeypatch, written: dict[str, str | None]):
    """sdkmanager 'installs' by writing each package's source.properties (None: writes nothing)."""
    monkeypatch.setattr(sdk_setup, "SDK_ROOT", tmp_path / "sdk")
    monkeypatch.setattr(sdk_setup, "_sdkmanager", lambda java_home: (["sdkmanager.bat"], {}))

    def call(command, env=None):
        assert command[1:] == list(sdk_setup.INSTALL)
        for name, text in written.items():
            if text is not None:
                folder = sdk_setup.SDK_ROOT.joinpath(*name.split(";"))
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "source.properties").write_text(text, encoding="utf-8")
        return 0

    monkeypatch.setattr(sdk_setup.subprocess, "call", call)
    return sdk_setup.install(tmp_path / "jdk")


def props(revision: str) -> str:
    return f"Pkg.Desc=fixture\nPkg.Revision={revision}\nPkg.Path=x\n"


def test_the_pins_are_the_committed_selection():
    assert {name: sdk_setup.PINS["packages"][name]["revision"] for name in sdk_setup.INSTALL} == PINNED


def test_matching_revisions_are_accepted(tmp_path, monkeypatch):
    assert fake_install(tmp_path, monkeypatch, {name: props(rev) for name, rev in PINNED.items()}) == 0


@pytest.mark.parametrize("written", [
    {"platforms;android-36": props("3"), "build-tools;36.0.0": props("36.0.0")},       # a newer platform revision
    {"platforms;android-36": props("2"), "build-tools;36.0.0": props("36.1.0")},       # other build-tools
    {"platforms;android-36": props("2"), "build-tools;36.0.0": None},                  # missing metadata
    {"platforms;android-36": "Pkg.Desc=no revision\n", "build-tools;36.0.0": props("36.0.0")},  # no Pkg.Revision
    {"platforms;android-36": props("two"), "build-tools;36.0.0": props("36.0.0")},     # malformed revision
], ids=["platform-r3", "build-tools-36.1.0", "missing", "no-revision", "malformed"])
def test_a_wrong_missing_or_malformed_revision_is_a_failure(tmp_path, monkeypatch, written):
    assert fake_install(tmp_path, monkeypatch, written) == 1
