"""Account persistence for the invite-only staging pilot."""

from secrets import token_urlsafe
from time import time
from uuid import uuid4

from argon2.exceptions import VerifyMismatchError

from .accounts import PASSWORD_HASHER, PASSWORD_WORK, SESSION_SECONDS, TOKEN, digest

INVITE_SECONDS = 48 * 60 * 60
RESET_SECONDS = 30 * 60


class PilotAccounts:
    def __init__(self, store, allowed_emails):
        self.store = store
        self.allowed_emails = frozenset(email.strip().lower() for email in allowed_emails)
        with store.session() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pilot_accounts (
                    email TEXT PRIMARY KEY, workspace TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pilot_links (
                    email TEXT NOT NULL, purpose TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE, expires REAL NOT NULL,
                    PRIMARY KEY (email, purpose)
                );
                CREATE TABLE IF NOT EXISTS pilot_sessions (
                    digest TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES pilot_accounts(email),
                    expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pilot_sessions_by_email ON pilot_sessions(email);
            """)

    def issue_invite(self, email):
        return self.issue_link(email, "invite", INVITE_SECONDS)

    def issue_reset(self, email):
        return self.issue_link(email, "reset", RESET_SECONDS)

    def issue_link(self, email, purpose, lifetime):
        email = email.strip().lower()
        if email not in self.allowed_emails:
            return None
        with self.store.session(write=True) as db:
            registered = db.execute("SELECT 1 FROM pilot_accounts WHERE email = ?", (email,)).fetchone()
            if bool(registered) != (purpose == "reset"):
                return None
            token = token_urlsafe(32)
            db.execute("""
                INSERT INTO pilot_links (email, purpose, digest, expires) VALUES (?, ?, ?, ?)
                ON CONFLICT (email, purpose) DO UPDATE SET digest = excluded.digest, expires = excluded.expires
            """, (email, purpose, digest(token), time() + lifetime))
        return token

    def enroll(self, token, password):
        return self.redeem(token, password, "invite")

    def reset(self, token, password):
        return self.redeem(token, password, "reset")

    def redeem(self, token, password, purpose):
        if not TOKEN.fullmatch(token) or not 12 <= len(password) <= 128:
            return False
        token_digest = digest(token)
        with self.store.session() as db:
            link = self.find_link(db, token_digest, purpose)
        if link is None or not PASSWORD_WORK.acquire(blocking=False):
            return False
        try:
            password_hash = PASSWORD_HASHER.hash(password)
        finally:
            PASSWORD_WORK.release()
        with self.store.session(write=True) as db:
            link = self.find_link(db, token_digest, purpose)
            if link is None:
                return False
            email = link["email"]
            if purpose == "invite":
                db.execute("INSERT INTO pilot_accounts VALUES (?, ?, ?)",
                           (email, f"pilot-{uuid4()}", password_hash))
            else:
                db.execute("UPDATE pilot_accounts SET password_hash = ? WHERE email = ?", (password_hash, email))
                db.execute("DELETE FROM pilot_sessions WHERE email = ?", (email,))
            db.execute("DELETE FROM pilot_links WHERE digest = ?", (token_digest,))
        return True

    def find_link(self, db, token_digest, purpose):
        link = db.execute("SELECT email FROM pilot_links WHERE digest = ? AND purpose = ? AND expires > ?",
                          (token_digest, purpose, time())).fetchone()
        return link if link and link["email"] in self.allowed_emails else None

    def authenticate(self, email, password):
        email = email.strip().lower()
        if email not in self.allowed_emails or not 1 <= len(password) <= 128:
            return None
        if not PASSWORD_WORK.acquire(blocking=False):
            return None
        try:
            with self.store.session() as db:
                row = db.execute("SELECT password_hash FROM pilot_accounts WHERE email = ?", (email,)).fetchone()
            if not row:
                return None
            password_hash = row["password_hash"]
            try:
                PASSWORD_HASHER.verify(password_hash, password)
            except VerifyMismatchError:
                return None
        finally:
            PASSWORD_WORK.release()
        with self.store.session(write=True) as db:
            # The password may have changed while the verification was running.
            if not db.execute("SELECT 1 FROM pilot_accounts WHERE email = ? AND password_hash = ?",
                              (email, password_hash)).fetchone():
                return None
            token = token_urlsafe(32)
            now = time()
            db.execute("DELETE FROM pilot_sessions WHERE expires <= ?", (now,))
            db.execute("INSERT INTO pilot_sessions VALUES (?, ?, ?)", (digest(token), email, now + SESSION_SECONDS))
        return token

    def workspace(self, token):
        if not TOKEN.fullmatch(token):
            return None
        with self.store.session() as db:
            row = db.execute("""
                SELECT a.email, a.workspace FROM pilot_sessions s JOIN pilot_accounts a ON a.email = s.email
                WHERE s.digest = ? AND s.expires > ?
            """, (digest(token), time())).fetchone()
        return row["workspace"] if row and row["email"] in self.allowed_emails else None

    def logout(self, token):
        with self.store.session(write=True) as db:
            db.execute("DELETE FROM pilot_sessions WHERE digest = ?", (digest(token),))
