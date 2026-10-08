"""Build a standalone, narrowly dispatched Qronos backend for Tauri.

Run on the target OS using the project's pinned runtime dependencies. Only
product modules and the sanitized brain configuration are inputs. Models,
native voice assets, user state, test/debug tools and credentials are not
silently collected from the development machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import sysconfig

from build_support import cpython_notices
from build_support.runtime_notices import collect_runtime_notices, prune_portaudio


ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_VERSION = 1
_PYTHON_KEYS = {"implementation", "version", "platform", "build", "sourceCommit", "auditedRecord", "packagingTarget"}


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bundle_files(bundle: Path) -> tuple[Path, ...]:
    """Walk without following links, including Windows junctions/reparse points."""
    def require_regular_path(path: Path) -> None:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & 0x400:
            raise ValueError("A runtime bundle may not contain links or reparse points.")

    require_regular_path(bundle)
    if not bundle.is_dir():
        raise ValueError("The runtime bundle directory is missing.")
    files: list[Path] = []
    for directory, subdirectories, names in os.walk(bundle, followlinks=False):
        current = Path(directory)
        for name in subdirectories:
            require_regular_path(current / name)
        for name in names:
            path = current / name
            require_regular_path(path)
            if path != bundle / "runtime-manifest.json":
                if not path.is_file():
                    raise ValueError("The runtime bundle contains an unsupported file type.")
                if path.suffix.lower() in {".onnx", ".gguf", ".safetensors", ".tflite", ".pt", ".bin"} or name.lower().endswith(".onnx.data"):
                    raise ValueError("Model assets require separate approved provisioning.")
                files.append(path)
    return tuple(sorted(files))


def packaging_python(*, allow_audited_interpreter: bool = False) -> dict:
    """The audited identity of the build interpreter, refused before any build work.

    Release bundles must use the CPython packaging target named in the
    registry; another audited interpreter needs an explicit opt-in and is
    recorded as not the target.
    """
    return cpython_notices.packaging_identity(
        cpython_notices.load_registry(),
        cpython_notices.current_interpreter(),
        allow_audited=allow_audited_interpreter,
    )


def write_manifest(bundle: Path, python: dict | None = None) -> Path:
    executable = "qronos-runtime.exe" if os.name == "nt" else "qronos-runtime"
    if not (bundle / executable).is_file():
        raise ValueError("The standalone runtime executable is missing.")
    entries = []
    for path in bundle_files(bundle):
        relative = path.relative_to(bundle).as_posix()
        entries.append({"path": relative, "size": path.stat().st_size, "sha256": hash_file(path)})
    manifest = bundle / "runtime-manifest.json"
    content = {
        "protocolVersion": PROTOCOL_VERSION,
        "executable": executable,
        "pythonVersion": ".".join(map(str, sys.version_info[:3])),
    }
    if python is not None:
        _check_python_identity(python, content["pythonVersion"])
        content["python"] = dict(sorted(python.items()))
    content["files"] = entries
    manifest.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    return manifest


def _check_python_identity(python: object, version: object) -> None:
    if not isinstance(python, dict) or set(python) != _PYTHON_KEYS or type(python["packagingTarget"]) is not bool:
        raise ValueError("Invalid runtime manifest interpreter identity.")
    if not all(isinstance(python[key], str) and python[key] for key in _PYTHON_KEYS - {"packagingTarget"}):
        raise ValueError("Invalid runtime manifest interpreter identity.")
    if python["version"] != version:
        raise ValueError("The runtime manifest interpreter identity does not match its Python version.")


def verify_manifest(bundle: Path) -> None:
    actual = {path.relative_to(bundle).as_posix() for path in bundle_files(bundle)}
    raw = json.loads((bundle / "runtime-manifest.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("protocolVersion") != PROTOCOL_VERSION:
        raise ValueError("Unsupported bundled runtime protocol.")
    expected_executable = "qronos-runtime.exe" if os.name == "nt" else "qronos-runtime"
    if "python" in raw:
        _check_python_identity(raw["python"], raw.get("pythonVersion"))
    entries = raw.get("files")
    if raw.get("executable") != expected_executable or not isinstance(entries, list) or not entries:
        raise ValueError("The runtime manifest does not describe the complete bundle.")
    seen: set[str] = set()
    normalized: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid runtime manifest entry.")
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
            raise ValueError("Invalid or duplicate runtime manifest path.")
        parts = PurePosixPath(relative)
        if parts.is_absolute() or relative != parts.as_posix() or any(p in {"", ".", ".."} for p in relative.split("/")):
            raise ValueError("Invalid or duplicate runtime manifest path.")
        if relative.casefold() in normalized:
            raise ValueError("Invalid or duplicate runtime manifest path.")
        normalized.add(relative.casefold())
        seen.add(relative)
        path = bundle.joinpath(*parts.parts)
        if relative not in actual:
            raise ValueError(f"Bundled runtime file is missing: {relative}")
        size, digest = entry.get("size"), entry.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid runtime manifest integrity metadata.")
        if path.stat().st_size != size or hash_file(path) != digest:
            raise ValueError(f"Bundled runtime file failed integrity verification: {relative}")
    if actual != seen or raw.get("executable") not in seen:
        raise ValueError("The runtime manifest does not describe the complete bundle.")


def build(destination: Path, *, allow_audited_interpreter: bool = False) -> Path:
    destination = destination.resolve()
    if destination == ROOT or destination in ROOT.parents:
        raise ValueError("The runtime destination must not replace the source checkout.")
    bundle = destination / "qronos-runtime"
    if bundle.exists():
        raise ValueError("Runtime output already exists. Choose a new empty destination.")
    # Only the x64 PortAudio build is shipped, so refuse other Windows targets
    # before any PyInstaller work starts.
    if os.name == "nt" and sysconfig.get_platform() != "win-amd64":
        raise ValueError("The packaged Windows backend supports x64 builds only.")
    # The interpreter must be an audited CPython build (the packaging target
    # unless explicitly allowed otherwise) before anything is created.
    python = packaging_python(allow_audited_interpreter=allow_audited_interpreter)
    # Separate per-build analysis directories prevent stale spec/data reuse.
    destination.mkdir(parents=True, exist_ok=True)
    work = destination / "build-metadata"
    command = [
        sys.executable, "-m", "PyInstaller", "--onedir", "--console",
        "--name", "qronos-runtime", "--distpath", str(destination),
        "--workpath", str(work / "analysis"), "--specpath", str(work / "spec"),
        "--paths", str(ROOT), "--collect-submodules", "core",
        "--collect-submodules", "security", "--hidden-import", "openwakeword.model",
        "--hidden-import", "sounddevice",
        "--add-data", f"{ROOT / 'config' / 'brains.json'}:config",
    ]
    for excluded in ("pytest", "tests", "tools", "torch", "torchaudio", "torchvision", "tensorflow", "matplotlib"):
        command.extend(["--exclude-module", excluded])
    command.append(str(ROOT / "build_support" / "runtime_entry.py"))
    # PyInstaller module discovery imports product modules. Those imports must
    # not read the builder's persisted profile or create developer artifacts.
    environment = dict(os.environ)
    profile = work / "analysis-profile"
    environment.update({
        "LOCALAPPDATA": str(profile / "local"),
        "APPDATA": str(profile / "roaming"),
        "USERPROFILE": str(profile / "user"),
        "HOME": str(profile / "user"),
        "QRONOS_SETTINGS_PATH": str(profile / "settings.json"),
        "QRONOS_CONVERSATION_HISTORY_PATH": str(profile / "history.sqlite3"),
        "QRONOS_PERMISSION_POLICY_PATH": str(profile / "permissions.json"),
        "QRONOS_PERMISSION_AUDIT_PATH": str(profile / "audit.jsonl"),
        "QRONOS_APPROVAL_SECRET_PATH": str(profile / "approval-secret.sqlite3"),
        "QRONOS_QUEUE_STATE_PATH": str(profile / "queue-state.json"),
        "QRONOS_MEMORY_RUNTIME": "0",
        "QRONOS_DESKTOP_WAKE_BOOTSTRAP": "0",
    })
    for override in ("QRONOS_PACKAGED_RUNTIME", "QRONOS_DATA_ROOT", "QRONOS_ASSET_ROOT", "QRONOS_BRAIN_CONFIG"):
        environment.pop(override, None)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    # Notices are added and unused PortAudio builds removed before the
    # manifest is written, so the manifest describes exactly what ships.
    pruned: tuple[str, ...] = ()
    if os.name == "nt":
        pruned = prune_portaudio(bundle)
    collect_runtime_notices(bundle, pruned=pruned)
    write_manifest(bundle, python)
    verify_manifest(bundle)
    # Use a separate disposable profile; never import the developer's user data.
    environment.update({
        "QRONOS_PACKAGED_RUNTIME": "1",
        "QRONOS_DATA_ROOT": str(work / "smoke-profile"),
        "LOCALAPPDATA": str(work / "smoke-local"),
        "QRONOS_MEMORY_RUNTIME": "0",
        "QRONOS_DESKTOP_WAKE_BOOTSTRAP": "0",
    })
    executable = bundle / ("qronos-runtime.exe" if os.name == "nt" else "qronos-runtime")
    result = subprocess.run([str(executable), "--self-check"], cwd=work, env=environment,
                            capture_output=True, text=True, timeout=90, check=True)
    snapshot = json.loads(result.stdout)
    if snapshot.get("ok") is not True or snapshot.get("packaged") is not True:
        raise ValueError("The standalone backend did not pass its import smoke test.")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "runtime" / "package")
    parser.add_argument("--verify", type=Path, help="Verify an existing standalone bundle instead of building.")
    parser.add_argument(
        "--allow-audited-interpreter", action="store_true",
        help="Development bundles only: accept an audited CPython build that is not the packaging target.",
    )
    args = parser.parse_args()
    if args.verify:
        verify_manifest(args.verify.resolve())
        print("Runtime bundle integrity verified.")
    else:
        print(build(args.destination, allow_audited_interpreter=args.allow_audited_interpreter))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
