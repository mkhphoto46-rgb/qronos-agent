from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid
import webbrowser

from core.path_safety import is_link_or_reparse_point, same_path
from core.storage_locations import (
    StorageLocationError,
    read_storage_state,
    validate_location,
)

HASH_INLINE_MAX_BYTES = 128 * 1024 * 1024

# Extensions Windows executes, or hands to a script host, when a file is opened
# through its shell association. The Library never opens one and never lets a
# rename produce one: "open" on such an artifact would be arbitrary code
# execution, which is hard-denied by policy.
EXECUTABLE_EXTENSIONS = frozenset({
    "exe","com","bat","cmd","ps1","psm1","psd1","ps1xml","psc1","vbs","vbe","vb","js","jse",
    "wsf","wsh","ws","wsc","msi","msp","mst","scr","pif","lnk","url","hta","cpl","jar",
    "reg","dll","sys","drv","ocx","appx","appxbundle","msix","msixbundle","appref-ms",
    "application","gadget","inf","scf","py","pyw","pyz","pyc","sct","msc","chm",
    "settingcontent-ms","library-ms","search-ms","diagcab","xbap","iso","img","vhd","vhdx",
})

_IMAGE = {"png","jpg","jpeg","webp","gif","bmp","tif","tiff","svg"}
_VIDEO = {"mp4","mkv","mov","avi","webm","m4v"}
_AUDIO = {"wav","mp3","flac","ogg","m4a","aac","opus"}
_ARCHIVE = {"zip","7z","rar","tar","gz","bz2","xz"}
_DOCUMENT = {"doc","docx","txt","md","json","csv","xls","xlsx","ppt","pptx","xml","yaml","yml","html","htm","rtf","py","js","ts","css","sql"}


def classify(path_or_url: str) -> tuple[str, str, str, str]:
    if path_or_url.startswith(("http://", "https://")):
        return "link", "LINK", "link", "text/uri-list"
    suffix = Path(path_or_url).suffix.lower().lstrip(".")
    ext = suffix.upper() or "FILE"
    mime = mimetypes.guess_type(path_or_url)[0] or "application/octet-stream"
    if suffix in _IMAGE:
        return "image", ext, "vector" if suffix == "svg" else "image", mime
    if suffix in _VIDEO:
        return "video", ext, "video", mime
    if suffix == "pdf":
        return "pdf", "PDF", "pdf", mime
    if suffix in _AUDIO:
        return "audio", ext, "audio", mime
    if suffix in _ARCHIVE:
        return "archive", ext, "archive", mime
    icon = {
        "doc":"word","docx":"word","md":"markdown","json":"json",
        "csv":"spreadsheet","xls":"spreadsheet","xlsx":"spreadsheet",
        "ppt":"presentation","pptx":"presentation","txt":"text",
        "py":"code","js":"code","ts":"code","css":"code","html":"code","htm":"code","sql":"code","xml":"code","yaml":"code","yml":"code",
    }.get(suffix, "document")
    return ("document" if suffix in _DOCUMENT else "unknown"), ext, icon, mime


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def is_executable_name(name: str) -> bool:
    """True when the shell would run, rather than display, a file of this name."""
    cleaned = name.strip().rstrip(". ").lower()
    return any(cleaned.endswith("." + ext) for ext in EXECUTABLE_EXTENSIONS)


# is_link_or_reparse_point and same_path live in core.path_safety and are
# re-exported here for existing callers. Links are symlinks, junctions, mount
# points and app aliases; cloud-sync placeholders are not links.
def _file_stat(path: Path) -> os.stat_result:
    # One seam for identity reads, so tests can reproduce file-number reuse
    # deterministically on any platform.
    return os.stat(path)


def _state_marker(st: os.stat_result) -> tuple[str, int]:
    """
    A timestamp the user cannot set with utime: the birth time where the
    platform reports one, otherwise the inode change time (POSIX st_ctime),
    which the kernel resets whenever a file is created or its inode changes.
    """
    birth = getattr(st, "st_birthtime_ns", None)
    if birth is not None:
        return "birth", int(birth)
    return "ctime", int(st.st_ctime_ns)


def file_identity(path: Path) -> dict[str, object]:
    """
    What identifies the exact file Qronos registered.

    A volume/file number alone is not identity: Linux file systems routinely
    give a file created after a delete the number the deleted file had, so a
    replaced file would look like the original. Identity is therefore either
    the exact content (SHA-256, for files up to HASH_INLINE_MAX_BYTES) or, for
    larger files that are not hashed, the exact recorded file state: volume,
    file number, size, modification time and a marker the user cannot set.
    """
    st = _file_stat(path)
    size = int(st.st_size)
    marker_kind, marker = _state_marker(st)
    return {
        "size": size,
        "sha256": _sha256(path) if size <= HASH_INLINE_MAX_BYTES else "",
        "dev": int(st.st_dev),
        "ino": int(st.st_ino),
        "mtime_ns": int(st.st_mtime_ns),
        "marker_kind": marker_kind,
        "marker_ns": marker,
    }


def _same_file_state(recorded: dict[str, object], st: os.stat_result) -> bool:
    """Exact unchanged state of an unhashed file. Missing fields never match."""
    marker_kind, marker = _state_marker(st)
    try:
        return (
            int(recorded["ino"]) != 0
            and int(recorded["dev"]) == int(st.st_dev)
            and int(recorded["ino"]) == int(st.st_ino)
            and int(recorded["size"]) == int(st.st_size)
            and int(recorded["mtime_ns"]) == int(st.st_mtime_ns)
            and str(recorded["marker_kind"]) == marker_kind
            and int(recorded["marker_ns"]) == marker
        )
    except (KeyError, TypeError, ValueError):
        # Records written before this state was kept (or damaged ones) cannot
        # prove anything about a large file: fail closed.
        return False


def identity_matches(recorded: dict[str, object], path: Path, *, content_hash: str = "") -> bool:
    """
    True only when the file at ``path`` is provably the recorded artifact.

    Hashed artifacts must have exactly the recorded bytes: copies and
    cross-volume moves keep them, in-place edits and replacements do not, and
    a reused file number proves nothing. Unhashed (large) artifacts must be in
    exactly the recorded file state; they are never hashed here, and one that
    has grown past the hashing limit since registration is refused.
    """
    st = _file_stat(path)
    size = int(st.st_size)
    expected = str(recorded.get("sha256") or content_hash or "")
    if expected:
        recorded_size = recorded.get("size")
        try:
            if recorded_size is not None and int(recorded_size) != size:
                return False
        except (TypeError, ValueError):
            return False
        if size > HASH_INLINE_MAX_BYTES:
            return False
        return _sha256(path) == expected
    return _same_file_state(recorded, st)


@dataclass(frozen=True)
class Artifact:
    id: str
    name: str
    category: str
    extension: str
    icon_key: str
    mime_type: str
    path_or_url: str
    size_bytes: int
    created_at: float
    modified_at: float
    origin: str
    content_hash: str
    duplicate_group_id: str
    availability_status: str
    duplicate_state: str
    read_state: str
    source_conversation_id: str
    source_message_id: str
    source_request_id: str
    thumbnail_path: str
    last_verified_at: float
    link_error_reason: str
    # Not public: the registry's private record of which file it registered.
    file_identity: str = ""

    def identity(self) -> dict[str, object]:
        try:
            parsed = json.loads(self.file_identity) if self.file_identity else {}
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "extension": self.extension,
            "iconKey": self.icon_key,
            "mimeType": self.mime_type,
            "pathOrUrl": self.path_or_url,
            "sizeBytes": self.size_bytes,
            "createdAt": self.created_at,
            "modifiedAt": self.modified_at,
            "origin": self.origin,
            "contentHash": self.content_hash,
            "duplicateGroupId": self.duplicate_group_id,
            "availabilityStatus": self.availability_status,
            "duplicateState": self.duplicate_state,
            "readState": self.read_state,
            "sourceConversationId": self.source_conversation_id,
            "sourceMessageId": self.source_message_id,
            "sourceRequestId": self.source_request_id,
            "thumbnailPath": self.thumbnail_path,
            "lastVerifiedAt": self.last_verified_at,
            "linkErrorReason": self.link_error_reason,
        }


class ArtifactRegistry:
    def __init__(
        self,
        path: str | Path | None = None,
        artifact_root: str | Path | None = None,
    ) -> None:
        # Explicit locations (tests, embedded callers) must not touch the
        # user's real storage locator.
        # The locator is read (never written) only for a location the caller
        # did not supply; only the two roots this registry uses are created.
        loc = read_storage_state().locations if path is None or artifact_root is None else None
        if loc is not None and ((path is None and not loc.library_db_root) or (artifact_root is None and not loc.artifact_root)):
            raise StorageLocationError(
                "unavailable",
                "The Library has no usable folder; choose one for the Library first.",
            )
        self.path = Path(path) if path is not None else Path(loc.library_db_root) / "library.sqlite3"
        self.artifact_root = Path(artifact_root) if artifact_root is not None else Path(loc.artifact_root)
        # Both roots this registry creates pass the storage policy, explicit
        # ones included, so it can never create folders in the program folder,
        # the working directory or a system folder.
        validate_location("libraryDbRoot", str(self.path.parent))
        validate_location("artifactRoot", str(self.artifact_root))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._ensure_welcome_artifact()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connection() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    icon_key TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    path_or_url TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    modified_at REAL NOT NULL,
                    origin TEXT NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT '',
                    duplicate_group_id TEXT NOT NULL DEFAULT '',
                    availability_status TEXT NOT NULL DEFAULT 'AVAILABLE',
                    duplicate_state TEXT NOT NULL DEFAULT 'UNIQUE',
                    read_state TEXT NOT NULL DEFAULT 'NEW',
                    source_conversation_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    source_request_id TEXT NOT NULL DEFAULT '',
                    thumbnail_path TEXT NOT NULL DEFAULT '',
                    last_verified_at REAL NOT NULL DEFAULT 0,
                    link_error_reason TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {str(row[1]) for row in c.execute("PRAGMA table_info(artifacts)").fetchall()}
            if "file_identity" not in columns:
                c.execute("ALTER TABLE artifacts ADD COLUMN file_identity TEXT NOT NULL DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_hash ON artifacts(content_hash)")
            # Approvals for Library mutations must survive between the separate
            # prepare and commit CLI processes. Only a hash of each token is
            # stored, and each row is consumed exactly once.
            c.execute("""
                CREATE TABLE IF NOT EXISTS library_pending_actions (
                    token_hash TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)

    def _ensure_welcome_artifact(self) -> None:
        with self._connection() as c:
            count = int(c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        if count:
            return
        docs = self.artifact_root / "Documents"
        docs.mkdir(parents=True, exist_ok=True)
        path = docs / "Qronos_Library_Welcome.md"
        if not path.exists():
            path.write_text(
                "# Qronos Library\n\nThis real file was created by Qronos to initialize the artifact library.\n",
                encoding="utf-8",
            )
        self.register_file(path, origin="Generated by Qronos")

    def register_file(
        self,
        path: str | Path,
        *,
        origin: str = "Generated by Qronos",
        source_conversation_id: str = "",
        source_message_id: str = "",
        source_request_id: str = "",
        unread: bool = True,
    ) -> Artifact:
        raw = Path(path).expanduser()
        if is_link_or_reparse_point(raw):
            raise PermissionError("Links and reparse points cannot be registered as artifacts.")
        p = raw.resolve()
        if not p.is_file():
            raise FileNotFoundError(str(p))
        category, ext, icon, mime = classify(str(p))
        st = p.stat()
        identity = file_identity(p)
        content_hash = str(identity["sha256"])
        identity_json = json.dumps(identity, sort_keys=True)
        now = time.time()
        artifact_id = f"art-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        target_id = artifact_id

        with self._connection() as c:
            existing = c.execute(
                "SELECT id FROM artifacts WHERE path_or_url = ?",
                (str(p),),
            ).fetchone()

            if existing:
                target_id = str(existing[0])
                c.execute(
                    """
                    UPDATE artifacts
                    SET name=?, category=?, extension=?, icon_key=?, mime_type=?,
                        size_bytes=?, modified_at=?, origin=?, content_hash=?,
                        file_identity=?,
                        read_state=?,
                        source_conversation_id=?,
                        source_message_id=?,
                        source_request_id=?,
                        availability_status='AVAILABLE',
                        link_error_reason='',
                        last_verified_at=?
                    WHERE id=?
                    """,
                    (
                        p.name,
                        category,
                        ext,
                        icon,
                        mime,
                        int(st.st_size),
                        float(st.st_mtime or now),
                        origin,
                        content_hash,
                        identity_json,
                        "NEW" if unread else "SEEN",
                        source_conversation_id,
                        source_message_id,
                        source_request_id,
                        now,
                        target_id,
                    ),
                )
            else:
                c.execute(
                    """
                    INSERT INTO artifacts (
                        id,name,category,extension,icon_key,mime_type,path_or_url,size_bytes,
                        created_at,modified_at,origin,content_hash,read_state,
                        source_conversation_id,source_message_id,source_request_id,last_verified_at,
                        file_identity
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        artifact_id,
                        p.name,
                        category,
                        ext,
                        icon,
                        mime,
                        str(p),
                        int(st.st_size),
                        float(st.st_ctime or now),
                        float(st.st_mtime or now),
                        origin,
                        content_hash,
                        "NEW" if unread else "SEEN",
                        source_conversation_id,
                        source_message_id,
                        source_request_id,
                        now,
                        identity_json,
                    ),
                )

        self._recompute_duplicates()
        return self.get(target_id)

    def register_link(self, url: str, *, name: str, origin: str = "Saved by Qronos", source_conversation_id: str = "", source_message_id: str = "", source_request_id: str = "") -> Artifact:
        cleaned = url.strip()
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("Only http/https links are supported.")
        now = time.time(); artifact_id=f"art-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        with self._connection() as c:
            c.execute("""INSERT INTO artifacts (id,name,category,extension,icon_key,mime_type,path_or_url,size_bytes,created_at,modified_at,origin,read_state,source_conversation_id,source_message_id,source_request_id,last_verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (artifact_id,name.strip() or cleaned,"link","LINK","link","text/uri-list",cleaned,0,now,now,origin,"NEW",source_conversation_id,source_message_id,source_request_id,now))
        return self.get(artifact_id)

    def _row(self, row: sqlite3.Row) -> Artifact:
        return Artifact(**{k: row[k] for k in Artifact.__dataclass_fields__.keys()})

    def get(self, artifact_id: str) -> Artifact:
        with self._connection() as c:
            row = c.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        return self._row(row)

    def list(self) -> list[Artifact]:
        self.refresh_health()
        with self._connection() as c:
            rows = c.execute("SELECT * FROM artifacts ORDER BY created_at DESC, id DESC").fetchall()
        return [self._row(row) for row in rows]

    def refresh_health(self) -> None:
        now=time.time()
        with self._connection() as c:
            rows=c.execute("SELECT id,category,path_or_url FROM artifacts").fetchall()
            for row in rows:
                if row["category"] == "link":
                    availability="AVAILABLE"; reason=""
                else:
                    exists=Path(row["path_or_url"]).is_file()
                    availability="AVAILABLE" if exists else "LINK_ERROR"
                    reason="" if exists else "file_missing"
                c.execute("UPDATE artifacts SET availability_status=?, link_error_reason=?, last_verified_at=? WHERE id=?",(availability,reason,now,row["id"]))
        self._recompute_duplicates()

    def _recompute_duplicates(self) -> None:
        with self._connection() as c:
            c.execute("UPDATE artifacts SET duplicate_state='UNIQUE', duplicate_group_id='' ")
            groups=c.execute("SELECT content_hash, COUNT(*) AS n FROM artifacts WHERE content_hash <> '' GROUP BY content_hash HAVING COUNT(*) > 1").fetchall()
            for row in groups:
                group_id=f"dup-{row['content_hash'][:16]}"
                c.execute("UPDATE artifacts SET duplicate_state='DUPLICATE', duplicate_group_id=? WHERE content_hash=?",(group_id,row['content_hash']))

    def mark_seen(self, ids: list[str]) -> None:
        clean=[x for x in ids if isinstance(x,str) and x.strip()]
        if not clean: return
        with self._connection() as c:
            c.executemany("UPDATE artifacts SET read_state='SEEN' WHERE id=?",[(x,) for x in clean])

    # Metadata-only mutation. The filesystem side of every Library change lives
    # in core.library_executor, which calls these only after the file operation
    # succeeded and was verified. Each update is conditional on the path the
    # executor verified, so a concurrent change is detected, not overwritten.

    def relocate(self, artifact_id: str, new_path: Path, *, expected_path: str) -> Artifact:
        p=Path(new_path); category,ext,icon,mime=classify(str(p)); st=p.stat(); identity=file_identity(p); now=time.time()
        with self._connection() as c:
            changed=c.execute(
                "UPDATE artifacts SET path_or_url=?, name=?, category=?, extension=?, icon_key=?, mime_type=?, size_bytes=?, modified_at=?, content_hash=?, file_identity=?, availability_status='AVAILABLE', link_error_reason='', last_verified_at=? WHERE id=? AND path_or_url=?",
                (str(p),p.name,category,ext,icon,mime,int(st.st_size),now,str(identity["sha256"]),json.dumps(identity,sort_keys=True),now,artifact_id,expected_path),
            ).rowcount
        if changed != 1: raise RuntimeError("Artifact metadata changed concurrently; nothing was updated.")
        self._recompute_duplicates(); return self.get(artifact_id)

    def rename_link(self, artifact_id: str, new_name: str, *, expected_path: str) -> Artifact:
        cleaned=new_name.strip()
        if not cleaned: raise ValueError("Name must not be empty.")
        with self._connection() as c:
            changed=c.execute("UPDATE artifacts SET name=?, modified_at=? WHERE id=? AND category='link' AND path_or_url=?",(cleaned,time.time(),artifact_id,expected_path)).rowcount
        if changed != 1: raise RuntimeError("Artifact metadata changed concurrently; nothing was updated.")
        return self.get(artifact_id)

    def remove(self, artifact_id: str, *, expected_path: str) -> None:
        with self._connection() as c:
            changed=c.execute("DELETE FROM artifacts WHERE id=? AND path_or_url=?",(artifact_id,expected_path)).rowcount
        if changed != 1: raise RuntimeError("Artifact metadata changed concurrently; nothing was removed.")
        self._recompute_duplicates()

    def put_pending_action(self, token_hash: str, operation: str, payload: dict[str, object], *, now: float, expires_at: float) -> None:
        with self._connection() as c:
            c.execute("DELETE FROM library_pending_actions WHERE expires_at <= ?",(now,))
            c.execute("INSERT INTO library_pending_actions (token_hash,operation,payload,created_at,expires_at) VALUES (?,?,?,?,?)",(token_hash,operation,json.dumps(payload,ensure_ascii=False,sort_keys=True),now,expires_at))

    def get_pending_action(self, token_hash: str) -> tuple[str, dict[str, object], float] | None:
        with self._connection() as c:
            row=c.execute("SELECT operation,payload,expires_at FROM library_pending_actions WHERE token_hash=?",(token_hash,)).fetchone()
        if row is None: return None
        payload=json.loads(row["payload"])
        return str(row["operation"]),(payload if isinstance(payload,dict) else {}),float(row["expires_at"])

    def consume_pending_action(self, token_hash: str) -> bool:
        """Remove one pending approval. True only for the one caller that removed it."""
        with self._connection() as c:
            return c.execute("DELETE FROM library_pending_actions WHERE token_hash=?",(token_hash,)).rowcount == 1

    def open(self, artifact_id: str) -> None:
        item=self.get(artifact_id)
        if item.category == "link": webbrowser.open(item.path_or_url); return
        path=Path(item.path_or_url)
        if is_link_or_reparse_point(path): raise PermissionError("Library does not open links or reparse points.")
        if not path.is_file(): raise FileNotFoundError(str(path))
        if not same_path(path.resolve(strict=True),path): raise PermissionError("Artifact path now resolves somewhere else; refusing to open it.")
        if is_executable_name(path.name): raise PermissionError("Library does not open executable or script files.")
        os.startfile(str(path))

    def open_folder(self, artifact_id: str | None = None) -> None:
        if artifact_id:
            item=self.get(artifact_id)
            if item.category == "link": raise ValueError("Web links do not have a Windows folder.")
            path=Path(item.path_or_url)
            if path.exists():
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                subprocess.Popen(["explorer.exe", str(path.parent)])
            return
        subprocess.Popen(["explorer.exe", str(self.artifact_root)])
