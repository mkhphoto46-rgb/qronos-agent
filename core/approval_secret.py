"""
Local verification of the user's approval secret.

TYPED_SECRET is the permission level for actions that need more than a click:
the person must type something only they know. A phrase printed in the dialog
does not qualify — anything that can read the dialog can type it. This module
holds the one thing that does: a secret the user set, stored only as a salted
scrypt digest, checked locally with a constant-time comparison.

What it guarantees, and what it does not:

    The plaintext is never stored, logged or returned, and neither is the
    derived digest. Callers get a yes/no, a lockout time, or a status.

    Guessing is rate-limited across processes. The Library and Permissions
    backends are short-lived CLI processes, so the failure counter lives in the
    same SQLite file as the credential and is updated in one IMMEDIATE
    transaction with the check. Parallel guesses serialise; none is lost.

    A credential is never silently replaced. Changing it requires the current
    secret, and a state file that is corrupt, unrecognised or written by a
    newer Qronos is refused rather than reinitialised.

    It is local approval verification, not protection against a compromised
    account. Anything running as the same Windows user can read or delete the
    state file, and a compromised webview can capture a secret while it is
    being typed into it. The first secret can be set without a prior one.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Callable, Iterator
import unicodedata
import uuid

from core.append_only_log import AppendOnlyLog


SCHEMA_VERSION = 1
KDF_NAME = "scrypt"

# Bounds on what a stored record may ask for. The lower bound stops a tampered
# or legacy record from downgrading verification to something cheap to brute
# force; the upper bound stops one from demanding gigabytes of memory.
SCRYPT_MIN_N = 2**15
SCRYPT_MAX_N = 2**17
SCRYPT_R = 8
SCRYPT_MAX_P = 2
DIGEST_BYTES = 32
SALT_BYTES = 16

MIN_SECRET_CHARS = 8
MAX_SECRET_CHARS = 256

MAX_FAILURES_BEFORE_LOCK = 5
BASE_LOCK_SECONDS = 60.0
MAX_LOCK_SECONDS = 3600.0

SETUP_HINT = (
    "No approval secret is set. Open Permissions and use APPROVAL SECRET to "
    "set one, then try again."
)

_PUBLIC_PHRASE = re.compile(r"(?i)^\s*delete\s*\d*\s*$")


class ApprovalSecretError(Exception):
    """Base class. Messages never contain submitted or stored secret material."""


class ApprovalSecretNotConfigured(ApprovalSecretError):
    def __init__(self) -> None:
        super().__init__(SETUP_HINT)


class ApprovalSecretLocked(ApprovalSecretError):
    def __init__(self, until: float) -> None:
        super().__init__(
            "Too many wrong approval secrets. Try again after the lockout ends."
        )
        self.until = float(until)


class ApprovalSecretUnavailable(ApprovalSecretError):
    """State is unreadable, unrecognised or newer; it was left untouched."""


class ApprovalSecretRejected(ApprovalSecretError):
    """The current secret was missing or wrong, so nothing was changed."""


class InvalidApprovalSecret(ApprovalSecretError):
    """A proposed new secret does not meet the minimum requirements."""


@dataclass(frozen=True)
class KdfParams:
    n: int = SCRYPT_MIN_N
    r: int = SCRYPT_R
    p: int = 1

    def validate(self) -> "KdfParams":
        if (
            self.n < SCRYPT_MIN_N
            or self.n > SCRYPT_MAX_N
            or self.n & (self.n - 1)
            or self.r != SCRYPT_R
            or not 1 <= self.p <= SCRYPT_MAX_P
        ):
            raise ApprovalSecretUnavailable(
                "The approval secret record uses unsupported key-derivation "
                "parameters; it was not used or changed."
            )
        return self


@dataclass(frozen=True)
class SecretStatus:
    configured: bool
    locked_until: float
    remaining_attempts: int

    def public(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "available": True,
            "lockedUntil": self.locked_until,
            "remainingAttempts": self.remaining_attempts,
        }


def approval_secret_path() -> Path:
    override = os.environ.get("QRONOS_APPROVAL_SECRET_PATH", "").strip()
    if override:
        return Path(override).expanduser()

    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Qronos" / "approval_secret.sqlite3"

    return Path.home() / ".qronos" / "approval_secret.sqlite3"


def _audit_path() -> Path:
    override = os.environ.get("QRONOS_PERMISSION_AUDIT_PATH", "").strip()
    if override:
        return Path(override).expanduser()

    from core.action_audit import DEFAULT_ACTION_AUDIT_PATH

    return Path(DEFAULT_ACTION_AUDIT_PATH)


def _encoded(secret: object) -> bytes:
    if not isinstance(secret, str):
        raise InvalidApprovalSecret("The approval secret must be text.")
    normalized = unicodedata.normalize("NFC", secret)
    if len(normalized) > MAX_SECRET_CHARS:
        raise InvalidApprovalSecret(
            f"The approval secret must be at most {MAX_SECRET_CHARS} characters."
        )
    return normalized.encode("utf-8")


def _derive(secret: bytes, salt: bytes, params: KdfParams) -> bytes:
    params.validate()
    return hashlib.scrypt(
        secret,
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=128 * params.r * (params.n + params.p + 2) + 1024 * 1024,
        dklen=DIGEST_BYTES,
    )


def _lock_seconds(failures: int) -> float:
    extra = max(0, failures - MAX_FAILURES_BEFORE_LOCK)
    return min(MAX_LOCK_SECONDS, BASE_LOCK_SECONDS * (2 ** min(extra, 16)))


class ApprovalSecretStore:
    """The user's approval secret: set, change, verify. Nothing else."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        kdf: KdfParams = KdfParams(),
        busy_timeout: float = 30.0,
        audit_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else approval_secret_path()
        self._clock = clock
        self._kdf = kdf.validate()
        self._timeout = float(busy_timeout)
        self._audit = AppendOnlyLog(
            Path(audit_path) if audit_path is not None else _audit_path()
        )

    # ---------------------------------------------------------------- public

    def status(self) -> SecretStatus:
        if not self.path.exists():
            return SecretStatus(False, 0.0, MAX_FAILURES_BEFORE_LOCK)
        with self._transaction(write=False) as c:
            credential = self._credential(c)
            failures, locked_until = self._attempts(c)
        now = float(self._clock())
        locked = locked_until > now
        return SecretStatus(
            configured=credential is not None,
            locked_until=locked_until if locked else 0.0,
            remaining_attempts=(
                0 if locked else max(1, MAX_FAILURES_BEFORE_LOCK - failures)
            ),
        )

    def verify(self, secret: object) -> bool:
        """
        True only for the configured secret.

        Raises when there is nothing to verify against, when verification is
        locked out, or when the state cannot be trusted. A wrong secret counts
        toward the persisted lockout before this returns False.
        """
        candidate = _encoded(secret)
        if not candidate:
            return False

        with self._transaction(write=True) as c:
            credential = self._credential(c)
            if credential is None:
                raise ApprovalSecretNotConfigured()
            ok = self._check(c, candidate, credential)

        if not ok:
            self._record("approval_secret_rejected", "Approval secret was wrong.")
        return ok

    def set_secret(
        self,
        new_secret: object,
        *,
        current_secret: object = None,
    ) -> SecretStatus:
        """
        Provision the first secret, or replace one with the current secret.

        This is an explicit user operation from the Permissions view. There is
        deliberately no reset: forgetting the secret means removing the state
        file outside Qronos, which is the same authority a same-user process
        already has.
        """
        proposed = _encoded(new_secret)
        text = proposed.decode("utf-8")
        if len(text) < MIN_SECRET_CHARS or not text.strip():
            raise InvalidApprovalSecret(
                f"The approval secret must be at least {MIN_SECRET_CHARS} characters."
            )
        if _PUBLIC_PHRASE.match(text):
            raise InvalidApprovalSecret(
                "A public confirmation phrase cannot be the approval secret."
            )

        with self._transaction(write=True) as c:
            credential = self._credential(c)
            changed = credential is not None

            if changed:
                current = _encoded(current_secret if current_secret is not None else "")
                if not current:
                    raise ApprovalSecretRejected(
                        "Enter the current approval secret to change it."
                    )
                rejected = not self._check(c, current, credential)
            else:
                rejected = False

            if not rejected:
                salt = secrets.token_bytes(SALT_BYTES)
                digest = _derive(proposed, salt, self._kdf)
                now = float(self._clock())
                c.execute("DELETE FROM credential")
                c.execute(
                    "INSERT INTO credential (id,kdf,n,r,p,salt,digest,created_at,changed_at) "
                    "VALUES (1,?,?,?,?,?,?,?,?)",
                    (
                        KDF_NAME,
                        self._kdf.n,
                        self._kdf.r,
                        self._kdf.p,
                        salt,
                        digest,
                        now if not changed else float(credential[4]),
                        now,
                    ),
                )

        if rejected:
            self._record(
                "approval_secret_rejected",
                "Approval secret change refused: current secret was wrong.",
            )
            raise ApprovalSecretRejected(
                "The current approval secret is wrong; nothing was changed."
            )

        self._record(
            "approval_secret_changed" if changed else "approval_secret_set",
            "Approval secret was changed." if changed else "Approval secret was set.",
        )
        return self.status()

    # -------------------------------------------------------------- internal

    def _check(
        self,
        c: sqlite3.Connection,
        candidate: bytes,
        credential: tuple[KdfParams, bytes, bytes, str, float, float],
    ) -> bool:
        """Compare inside the caller's IMMEDIATE transaction and count the result."""
        now = float(self._clock())
        failures, locked_until = self._attempts(c)
        if locked_until > now:
            raise ApprovalSecretLocked(locked_until)

        params, salt, digest = credential[0], credential[1], credential[2]
        ok = hmac.compare_digest(_derive(candidate, salt, params), digest)

        if ok:
            c.execute(
                "UPDATE attempts SET failures=0, locked_until=0, last_failure_at=0 WHERE id=1"
            )
            return True

        failures += 1
        locked_until = (
            now + _lock_seconds(failures)
            if failures >= MAX_FAILURES_BEFORE_LOCK
            else 0.0
        )
        c.execute(
            "UPDATE attempts SET failures=?, locked_until=?, last_failure_at=? WHERE id=1",
            (failures, locked_until, now),
        )
        return False

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            c = sqlite3.connect(
                str(self.path),
                timeout=self._timeout,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ApprovalSecretUnavailable(
                "The approval secret store could not be opened."
            ) from exc

        try:
            try:
                # IMMEDIATE takes the write lock up front, so two processes
                # cannot both read the same failure count and both "win".
                c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._ensure_schema(c, write=write)
            except ApprovalSecretError:
                raise
            except sqlite3.DatabaseError as exc:
                raise ApprovalSecretUnavailable(
                    "The approval secret store is unreadable or corrupt; it was "
                    "not used or overwritten."
                ) from exc

            try:
                yield c
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
            else:
                # A failed attempt must be counted even though the caller
                # then raises or returns False, so commit before returning.
                if c.in_transaction:
                    c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            c.close()

    def _ensure_schema(self, c: sqlite3.Connection, *, write: bool) -> None:
        tables = {
            str(row[0])
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if not tables:
            if not write:
                return
            c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.execute(
                "CREATE TABLE credential (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "kdf TEXT NOT NULL, n INTEGER NOT NULL, r INTEGER NOT NULL, "
                "p INTEGER NOT NULL, salt BLOB NOT NULL, digest BLOB NOT NULL, "
                "created_at REAL NOT NULL, changed_at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE attempts (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "failures INTEGER NOT NULL, locked_until REAL NOT NULL, "
                "last_failure_at REAL NOT NULL)"
            )
            c.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            c.execute("INSERT INTO attempts VALUES (1, 0, 0, 0)")
            return

        if not {"meta", "credential", "attempts"} <= tables:
            raise ApprovalSecretUnavailable(
                "The approval secret store is not recognised; it was not used "
                "or overwritten."
            )

        row = c.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        try:
            version = int(row[0]) if row is not None else -1
        except (TypeError, ValueError):
            version = -1

        if version > SCHEMA_VERSION:
            raise ApprovalSecretUnavailable(
                "The approval secret store was written by a newer Qronos; it "
                "was not used or overwritten."
            )
        if version != SCHEMA_VERSION:
            raise ApprovalSecretUnavailable(
                "The approval secret store has an unknown schema; it was not "
                "used or overwritten."
            )

        if c.execute("SELECT COUNT(*) FROM attempts WHERE id=1").fetchone()[0] != 1:
            raise ApprovalSecretUnavailable(
                "The approval secret attempt counter is missing; refusing to "
                "verify without it."
            )

    def _credential(
        self,
        c: sqlite3.Connection,
    ) -> tuple[KdfParams, bytes, bytes, str, float, float] | None:
        try:
            rows = c.execute(
                "SELECT kdf, n, r, p, salt, digest, created_at, changed_at FROM credential"
            ).fetchall()
        except sqlite3.OperationalError:
            return None  # read-only view of a store that does not exist yet

        if not rows:
            return None
        if len(rows) != 1:
            raise ApprovalSecretUnavailable("The approval secret record is invalid.")

        kdf, n, r, p, salt, digest, created_at, changed_at = rows[0]
        if (
            kdf != KDF_NAME
            or not isinstance(salt, bytes)
            or not isinstance(digest, bytes)
            or len(salt) != SALT_BYTES
            or len(digest) != DIGEST_BYTES
        ):
            raise ApprovalSecretUnavailable(
                "The approval secret record is invalid; it was not used or changed."
            )
        params = KdfParams(n=int(n), r=int(r), p=int(p)).validate()
        return params, salt, digest, kdf, float(created_at), float(changed_at)

    def _attempts(self, c: sqlite3.Connection) -> tuple[int, float]:
        try:
            row = c.execute(
                "SELECT failures, locked_until FROM attempts WHERE id=1"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0, 0.0
        if row is None:
            return 0, 0.0
        return int(row[0]), float(row[1])

    def _record(self, event: str, summary: str) -> None:
        self._audit.append(
            {
                "schemaVersion": 1,
                "at": round(float(self._clock()), 3),
                "event": event,
                "actionId": f"approval-secret-{uuid.uuid4().hex}",
                "category": "approval_secret",
                "target": "approval_secret",
                "summary": summary,
                "outcome": "refused" if event.endswith("rejected") else "succeeded",
                "decision": "",
            }
        )
