"""Private staging owner registration and browser sessions."""

import re
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from threading import BoundedSemaphore
from time import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SESSION_COOKIE = "__Host-agentcollab-session"
SESSION_SECONDS = 8 * 60 * 60
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,39}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
PASSWORD_WORK = BoundedSemaphore(2)


def digest(value):
    return sha256(value.encode()).hexdigest()


def return_path(value):
    if (not value.startswith("/") or value.startswith("//") or len(value) > 4096
            or any(ord(char) < 32 or char == "\\" for char in value)):
        return "/"
    try:
        target = urlsplit(value)
    except ValueError:
        return "/"
    if target.scheme or target.netloc or target.fragment or target.path not in {"/", "/agents", "/authorize", "/oauth/consent"}:
        return "/"
    return value


class OwnerAccounts:
    def __init__(self, store, setup_code, setup_expires):
        self.store = store
        self.setup_digest = digest(setup_code)
        self.setup_expires = setup_expires
        with store.session() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owner_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_sessions (
                    digest TEXT PRIMARY KEY, expires INTEGER NOT NULL
                );
            """)

    def exists(self):
        with self.store.session() as db:
            return db.execute("SELECT 1 FROM owner_account WHERE id = 1").fetchone() is not None

    def setup(self, code, username, password):
        if not TOKEN.fullmatch(code) or time() >= self.setup_expires or not compare_digest(digest(code), self.setup_digest):
            return None
        if not USERNAME.fullmatch(username) or not 12 <= len(password) <= 128:
            return None
        if self.exists() or not PASSWORD_WORK.acquire(blocking=False):
            return None
        try:
            password_hash = PASSWORD_HASHER.hash(password)
        finally:
            PASSWORD_WORK.release()
        with self.store.session(write=True) as db:
            if time() >= self.setup_expires:
                return None
            inserted = db.execute("INSERT OR IGNORE INTO owner_account VALUES (1, ?, ?)", (username, password_hash))
            return self.new_session(db) if inserted.rowcount == 1 else None

    def authenticate(self, username, password):
        if not 1 <= len(password) <= 128 or not PASSWORD_WORK.acquire(blocking=False):
            return None
        try:
            with self.store.session() as db:
                owner = db.execute("SELECT * FROM owner_account WHERE id = 1").fetchone()
            if owner is None:
                return None
            try:
                PASSWORD_HASHER.verify(owner["password_hash"], password)
            except VerifyMismatchError:
                return None
            if not compare_digest(username.lower().encode(), owner["username"].lower().encode()):
                return None
        finally:
            PASSWORD_WORK.release()
        with self.store.session(write=True) as db:
            return self.new_session(db)

    def new_session(self, db):
        now = int(time())
        token = token_urlsafe(32)
        db.execute("DELETE FROM owner_sessions WHERE expires <= ?", (now,))
        db.execute("INSERT INTO owner_sessions VALUES (?, ?)", (digest(token), now + SESSION_SECONDS))
        return token

    def valid(self, token):
        if not TOKEN.fullmatch(token):
            return False
        with self.store.session() as db:
            return db.execute("SELECT 1 FROM owner_sessions WHERE digest = ? AND expires > ?",
                              (digest(token), time())).fetchone() is not None

    def logout(self, token):
        with self.store.session(write=True) as db:
            db.execute("DELETE FROM owner_sessions WHERE digest = ?", (digest(token),))
