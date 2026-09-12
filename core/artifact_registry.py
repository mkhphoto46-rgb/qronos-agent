from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import uuid
import webbrowser

from core.storage_locations import load_storage_locations

HASH_INLINE_MAX_BYTES = 128 * 1024 * 1024

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
        loc = load_storage_locations(create=True)
        self.path = Path(path) if path is not None else Path(loc.library_db_root) / "library.sqlite3"
        self.artifact_root = Path(artifact_root) if artifact_root is not None else Path(loc.artifact_root)
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
            c.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_hash ON artifacts(content_hash)")

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
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(str(p))
        category, ext, icon, mime = classify(str(p))
        stat = p.stat()
        content_hash = _sha256(p) if stat.st_size <= HASH_INLINE_MAX_BYTES else ""
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
                        int(stat.st_size),
                        float(stat.st_mtime or now),
                        origin,
                        content_hash,
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
                        source_conversation_id,source_message_id,source_request_id,last_verified_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        artifact_id,
                        p.name,
                        category,
                        ext,
                        icon,
                        mime,
                        str(p),
                        int(stat.st_size),
                        float(stat.st_ctime or now),
                        float(stat.st_mtime or now),
                        origin,
                        content_hash,
                        "NEW" if unread else "SEEN",
                        source_conversation_id,
                        source_message_id,
                        source_request_id,
                        now,
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

    def rename(self, artifact_id: str, new_name: str) -> Artifact:
        item=self.get(artifact_id); cleaned=new_name.strip()
        if not cleaned: raise ValueError("Name must not be empty.")
        if item.category == "link":
            with self._connection() as c: c.execute("UPDATE artifacts SET name=?, modified_at=? WHERE id=?",(cleaned,time.time(),artifact_id))
            return self.get(artifact_id)
        src=Path(item.path_or_url); dest=src.with_name(cleaned)
        if dest.exists(): raise FileExistsError(str(dest))
        src.rename(dest)
        category,ext,icon,mime=classify(str(dest))
        with self._connection() as c:
            c.execute("UPDATE artifacts SET name=?, path_or_url=?, category=?, extension=?, icon_key=?, mime_type=?, modified_at=? WHERE id=?",(dest.name,str(dest),category,ext,icon,mime,time.time(),artifact_id))
        return self.get(artifact_id)

    def open(self, artifact_id: str) -> None:
        item=self.get(artifact_id)
        if item.category == "link": webbrowser.open(item.path_or_url); return
        path=Path(item.path_or_url)
        if not path.is_file(): raise FileNotFoundError(str(path))
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

    def copy(self, ids: list[str], destination: str) -> list[dict[str, object]]:
        dest=Path(destination); dest.mkdir(parents=True,exist_ok=True); results=[]
        for artifact_id in ids:
            try:
                item=self.get(artifact_id)
                if item.category == "link": raise ValueError("Links cannot be copied as files.")
                src=Path(item.path_or_url); target=dest/src.name
                if target.exists():
                    stem,suffix=target.stem,target.suffix; i=2
                    while target.exists(): target=dest/f"{stem} ({i}){suffix}"; i+=1
                shutil.copy2(src,target)
                created=self.register_file(target,origin="Copied by Qronos",source_conversation_id=item.source_conversation_id,source_message_id=item.source_message_id,source_request_id=item.source_request_id)
                results.append({"id":artifact_id,"ok":True,"newArtifactId":created.id})
            except Exception as exc: results.append({"id":artifact_id,"ok":False,"error":str(exc)})
        return results

    def move(self, ids: list[str], destination: str) -> list[dict[str, object]]:
        dest=Path(destination); dest.mkdir(parents=True,exist_ok=True); results=[]
        for artifact_id in ids:
            try:
                item=self.get(artifact_id)
                if item.category == "link": raise ValueError("Links cannot be moved as files.")
                src=Path(item.path_or_url); target=dest/src.name
                if target.exists(): raise FileExistsError(str(target))
                shutil.move(str(src),str(target))
                category,ext,icon,mime=classify(str(target)); stat=target.stat()
                with self._connection() as c:
                    c.execute("UPDATE artifacts SET path_or_url=?, name=?, category=?, extension=?, icon_key=?, mime_type=?, size_bytes=?, modified_at=?, availability_status='AVAILABLE', link_error_reason='' WHERE id=?",(str(target),target.name,category,ext,icon,mime,int(stat.st_size),time.time(),artifact_id))
                results.append({"id":artifact_id,"ok":True})
            except Exception as exc: results.append({"id":artifact_id,"ok":False,"error":str(exc)})
        self._recompute_duplicates(); return results

    def delete(self, ids: list[str], *, forever: bool) -> list[dict[str, object]]:
        results=[]
        for artifact_id in ids:
            try:
                item=self.get(artifact_id)
                if item.category != "link":
                    path=Path(item.path_or_url)
                    if path.exists():
                        if forever: path.unlink()
                        else: _send_to_recycle_bin(path)
                with self._connection() as c: c.execute("DELETE FROM artifacts WHERE id=?",(artifact_id,))
                results.append({"id":artifact_id,"ok":True})
            except Exception as exc: results.append({"id":artifact_id,"ok":False,"error":str(exc)})
        self._recompute_duplicates(); return results


def _send_to_recycle_bin(path: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Recycle Bin is supported on Windows only.")
    FO_DELETE=3; FOF_SILENT=0x0004; FOF_NOCONFIRMATION=0x0010; FOF_ALLOWUNDO=0x0040; FOF_NOERRORUI=0x0400
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_=[("hwnd",wintypes.HWND),("wFunc",wintypes.UINT),("pFrom",wintypes.LPCWSTR),("pTo",wintypes.LPCWSTR),("fFlags",wintypes.WORD),("fAnyOperationsAborted",wintypes.BOOL),("hNameMappings",ctypes.c_void_p),("lpszProgressTitle",wintypes.LPCWSTR)]
    op=SHFILEOPSTRUCTW(); op.wFunc=FO_DELETE; op.pFrom=str(path)+"\0\0"; op.fFlags=FOF_SILENT|FOF_NOCONFIRMATION|FOF_ALLOWUNDO|FOF_NOERRORUI
    result=ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted: raise OSError(f"Recycle Bin operation failed: {result}")
