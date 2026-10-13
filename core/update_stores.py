"""Closed, read-only registry of the data stores Qronos keeps, for update compatibility.

Each persistent store is versioned on its own; there is no global data version.
The updater compares this registry (what the running build reads and writes)
with the per-store ranges an owner-signed update package declares
(``dataStores`` in payload schema 2, see ``core.local_update``).

Nothing here migrates data. Forward migrations stay inside each store's own
reviewed code in the new build. The updater only:

* refuses packages that could not read the installed data, would write an older
  format, or belong to another compatibility epoch, and
* before handing off an upgrade that changes a store's format, keeps a bounded,
  hashed snapshot of that store's file when the store is a single fixed file in
  the data root (``snapshot``). Rolling back across such a change restores the
  snapshot; a store without one makes that rollback honestly unavailable.

The versions are literals so that this module imports nothing else; a test
compares each one with its store module's constant, so they cannot drift apart.
Unversioned stores (SQLite files whose tables are created by the store itself)
are listed explicitly, never ignored. Their "version 1" means exactly the
layout whose SQL statements hash to ``ddl_sha256`` (every CREATE/ALTER/DROP
string in the module, whitespace-normalised, sorted). A test recomputes it, so
changing such a layout without bumping the version here fails review. SQLite
stores have no snapshot: they are never copied raw, so an upgrade that changes
their format is refused by the updater (``unsupported_migration``).

Security registries (``sensitive``: paired devices with their shared secrets, the
permission policy) are never snapshotted either. Restoring an old copy would bring
back a device the person revoked or a permission they tightened, so an upgrade
that changes their format is refused rather than made "rollback-able" by a blind
restore.

A store whose file can be moved elsewhere by an environment variable
(``relocation_env``) is snapshotted only while that variable is unset, so the
snapshot is always of the file the store really uses; otherwise the update is
refused instead of silently recording a missing store.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.local_update import DataProfile

# Bumped only for a deliberate breaking change across stores; packages from
# another epoch are refused.
COMPATIBILITY_EPOCH = 1


@dataclass(frozen=True)
class StoreSpec:
    store_id: str
    version: int                 # the format this build reads and writes
    source: str                  # "module:CONSTANT" for versioned stores, "unversioned:<module>" otherwise
    snapshot: str | None         # file name in the data root when the store is that single file
    description: str
    ddl_sha256: str | None = None  # SQLite stores: fingerprint of the layout this version means
    reads_from: int = 1            # the oldest format this build's store code still reads (signed as reads[0])
    sensitive: bool = False        # security state: never restored from an old copy
    relocation_env: tuple[str, ...] = ()  # variables that move the store's file out of the data root


STORES: tuple[StoreSpec, ...] = (
    StoreSpec("settings", 2, "core.settings_service:SCHEMA_VERSION", "settings.json", "Settings",
              relocation_env=("QRONOS_SETTINGS_PATH",)),
    StoreSpec("conversations", 1, "core.conversation_store:SCHEMA_VERSION", "conversations.json", "Conversations"),
    StoreSpec("permissions", 1, "core.permission_control:SCHEMA_VERSION", None, "Permissions", sensitive=True),
    StoreSpec("link_devices", 1, "core.link_devices:SCHEMA_VERSION", None, "Paired devices", sensitive=True),
    StoreSpec("artifact_ownership", 1, "core.artifact_ownership:REGISTRY_FORMAT_VERSION",
              "artifact_ownership.json", "Library file ownership"),
    StoreSpec("undo_journal", 1, "core.undo_journal:SCHEMA_VERSION", "undo_journal.jsonl", "Undo journal"),
    StoreSpec("queue_state", 1, "core.queue_journal:QUEUE_STATE_VERSION", None, "Task queue"),
    StoreSpec("approval_secret", 1, "core.approval_secret:SCHEMA_VERSION", None, "Approval secret",
              "2671e131672d8ef6548b1fd606d2453d81ccf6c56b9004e622202aa7e9ef639d"),
    StoreSpec("storage_locations", 1, "core.storage_locations:LOCATOR_SCHEMA_VERSION", None, "Storage locations"),
    StoreSpec("actions", 1, "core.actions:SCHEMA_VERSION", None, "Pending actions"),
    StoreSpec("action_audit", 1, "core.action_audit:SCHEMA_VERSION", None, "Action audit log"),
    StoreSpec("user_memory", 1, "core.user_memory:MEMORY_SCHEMA_VERSION", None, "Memory (SQLite)",
              "9687e876cd70d9ecd38a1556eed8c14f9dd0fcecbdcf053aff174961e2285c49"),
    StoreSpec("app_workflow_journal", 1, "core.app_workflow:_JOURNAL_VERSION", None, "App workflow journal"),
    StoreSpec("security_monitor", 1, "core.security_monitor:STATE_VERSION", None, "Security monitor state"),
    StoreSpec("conversation_history", 1, "unversioned:core.conversation_history", None,
              "Conversation history (SQLite)", "5807990dd2a3865aa49668d6f2af095f8170387dd18536f4e2bd88bfee6225ec"),
    StoreSpec("artifact_registry", 1, "unversioned:core.artifact_registry", None, "Library index (SQLite)",
              "52f40c1e60b876d2654c3b32d37cf4621a1eb20679cd29704336a9261c300091"),
    StoreSpec("filename_index", 1, "unversioned:core.local_file_index", None, "File name index (SQLite)",
              "fff995b8f686a4f27361b7cf216955c91b72b0063daf20377b7ab5f6b5f3c4a4"),
    StoreSpec("web_cache", 1, "unversioned:core.web_cache", None, "Web page cache (SQLite)",
              "e6ff36875df600dcdf2ce936a7ba54cacfccb34616c2b404acc99332a933442e"),
    StoreSpec("web_friction", 1, "unversioned:core.web_friction", None, "Web friction memory (SQLite)",
              "be006f4135bd470f1da242fe441258599bf21f00530d4fa774573983ec670249"),
    # The media index is rebuildable from the user's files but is never copied raw: a package that changes its
    # format is refused (unsupported_migration), which is a release compatibility gate, not rollback support.
    # Schema 7 code (visual semantic) still reads (and migrates forward, once) schema-1 to -6 files.
    StoreSpec("media_index", 7, "qronos_media.store:SCHEMA_VERSION", None, "Media index (SQLite)",
              "5afba11e9639c72f7b346ab5cbfd1face673b955d564ff387582fe99bc657e77", reads_from=1,
              relocation_env=("QRONOS_MEDIA_DATA_DIR",)),
)

STORE_BY_ID = {spec.store_id: spec for spec in STORES}


def current_profile() -> DataProfile:
    """The data formats this build reads and writes."""
    return DataProfile({spec.store_id: spec.version for spec in STORES}, COMPATIBILITY_EPOCH)


def ddl_fingerprint(source: str) -> str:
    """The layout fingerprint of a SQLite store module's source (see ddl_sha256)."""
    import ast
    import hashlib
    import re

    statements = sorted({re.sub(r"\s+", " ", node.value).strip() for node in ast.walk(ast.parse(source))
                         if isinstance(node, ast.Constant) and isinstance(node.value, str)
                         and re.search(r"\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|UNIQUE|TRIGGER|VIEW)", node.value,
                                       re.I)})
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()


def changed_stores(profile: DataProfile, data_stores: dict[str, dict]) -> list[str]:
    """Stores whose format a package would write differently from the profile, sorted."""
    return sorted(store_id for store_id, version in profile.stores.items()
                  if store_id in data_stores and data_stores[store_id]["writes"] != version)
