"""Pilot credential provider. Replace this boundary for verified OIDC identities."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

from .errors import DemoError
from .identity import Principal
from .safe_paths import reject_links


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def private_file(path: Path, *, create: bool = False) -> None:
    reject_links(path)
    if os.name != "nt":
        if create:
            path.chmod(0o600 if path.is_file() else 0o700)
        elif path.stat().st_mode & 0o077:
            raise DemoError("AUTH_CONFIG_ACL", "Credential configuration must be owner-only.")
        return
    import win32api
    import win32con
    import win32security
    import ntsecuritycon

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    allowed = {win32security.ConvertSidToStringSid(sid), "S-1-5-18", "S-1-5-32-544"}
    if create:
        dacl = win32security.ACL()
        for value in allowed:
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION, 3 if path.is_dir() else 0, ntsecuritycon.FILE_ALL_ACCESS,
                win32security.ConvertStringSidToSid(value),
            )
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None,
        )
    security = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    dacl = security.GetSecurityDescriptorDacl()
    if dacl is None:
        raise DemoError("AUTH_CONFIG_ACL", "A restricted credential configuration ACL is required.")
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if ace[0][0] == win32security.ACCESS_DENIED_ACE_TYPE:
            continue
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or win32security.ConvertSidToStringSid(ace[2]) not in allowed:
            raise DemoError("AUTH_CONFIG_ACL", "Credential configuration allows an unexpected Windows identity.")


class PilotAuth:
    def __init__(self, config_file: Path, data_dir: Path, session_seconds: int = 3600):
        private_file(config_file)
        if config_file.stat().st_size > 128 * 1024:
            raise DemoError("AUTH_CONFIG", "Credential configuration exceeds its limit.")
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
            records = config["users"]
            self.users = {}
            hashes = set()
            for record in records:
                user = Principal(record["id"], record["display_name"], record.get("role", "user"))
                token_hash = record["token_sha256"]
                if user.id == "legacy" or user.id in self.users or token_hash in hashes or len(token_hash) != 64 or any(c not in "0123456789abcdef" for c in token_hash):
                    raise ValueError("Invalid identity or hash.")
                self.users[user.id] = (user, token_hash)
                hashes.add(token_hash)
            if not 1 <= len(self.users) <= 100:
                raise ValueError("Pilot requires 1..100 distinct identities.")
        except (ValueError, KeyError, TypeError) as exc:
            raise DemoError("AUTH_CONFIG", "Invalid pilot credential configuration.") from exc
        self.session_seconds = session_seconds
        self.database = data_dir / "auth.sqlite3"
        reject_links(self.database)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, user_id TEXT, csrf TEXT, expires REAL)")
        private_file(self.database, create=True)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def bearer(self, header: str | None) -> Principal:
        if not header or not header.startswith("Bearer ") or not 32 <= len(header[7:]) <= 256:
            raise DemoError("AUTH_REQUIRED", "A valid individual Bearer credential is required.")
        candidate = digest(header[7:])
        for user, expected in self.users.values():
            if hmac.compare_digest(candidate, expected):
                return user
        raise DemoError("AUTH_REQUIRED", "A valid individual Bearer credential is required.")

    def login(self, header: str | None):
        user = self.bearer(header)
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            # Bound active sessions independently of request upload quotas.
            if db.execute("SELECT count(*) FROM sessions WHERE user_id=?", (user.id,)).fetchone()[0] >= 10:
                db.execute("DELETE FROM sessions WHERE hash=(SELECT hash FROM sessions WHERE user_id=? ORDER BY expires LIMIT 1)", (user.id,))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest(session), user.id, csrf, time.time() + self.session_seconds))
        return user, session, csrf

    def session(self, token: str | None):
        if not token or len(token) > 128:
            raise DemoError("AUTH_REQUIRED", "Sign in to access your conversion tasks.")
        with self._connect() as db:
            row = db.execute("SELECT user_id,csrf FROM sessions WHERE hash=? AND expires>?", (digest(token), time.time())).fetchone()
        if not row or row[0] not in self.users:
            raise DemoError("AUTH_REQUIRED", "Your session has expired. Sign in again.")
        return self.users[row[0]][0], row[1]

    def logout(self, token: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))
