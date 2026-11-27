"""Persistence and authorization for the local messaging slice."""

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


AGENT_PROVIDERS = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "muse": "Muse",
    "perplexity": "Perplexity",
    "grok": "Grok",
    "copilot": "Copilot",
    "gemini": "Gemini",
    "other": "Other",
}


class StoreError(ValueError):
    """A rejected action with a message safe to show to the caller."""


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.session() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    token_hash TEXT UNIQUE,
                    disconnected INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_agent_names
                    ON agents(workspace, name_key) WHERE disconnected = 0;
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    sender TEXT NOT NULL REFERENCES agents(id),
                    recipient TEXT NOT NULL REFERENCES agents(id),
                    body TEXT NOT NULL,
                    request_key TEXT,
                    reply_to TEXT UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
                    created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    fetched TEXT,
                    UNIQUE(sender, request_key)
                );
                CREATE INDEX IF NOT EXISTS inbox_order ON messages(recipient, created, id);
                CREATE INDEX IF NOT EXISTS activity_order ON messages(workspace, created, id);
                CREATE TABLE IF NOT EXISTS oauth_issuer (
                    id INTEGER PRIMARY KEY CHECK (id = 1), signing_key BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_requests (
                    digest TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    digest TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_grants (
                    client_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id),
                    access_digest TEXT NOT NULL UNIQUE,
                    refresh_digest TEXT NOT NULL UNIQUE,
                    access_expires INTEGER NOT NULL,
                    grant_expires INTEGER NOT NULL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if not any(row["name"] == "provider" for row in db.execute("PRAGMA table_info(agents)")):
                db.execute("ALTER TABLE agents ADD COLUMN provider TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def session(self, *, write=False):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def active_agent(db, workspace, agent_id):
        agent = db.execute(
            "SELECT * FROM agents WHERE id = ? AND workspace = ? AND disconnected = 0",
            (agent_id, workspace),
        ).fetchone()
        if agent is None:
            raise StoreError("Agent is unavailable.")
        return agent

    def list_agents(self, workspace):
        with self.session() as db:
            return [dict(row, linked=bool(row["manual_credential"] or row["oauth_linked"])) for row in db.execute(
                """SELECT id, name, provider, token_hash IS NOT NULL AS manual_credential,
                   EXISTS (
                       SELECT 1 FROM oauth_grants WHERE agent_id = agents.id
                       AND grant_expires > CAST(strftime('%s', 'now') AS INTEGER)
                   ) AS oauth_linked FROM agents WHERE workspace = ? AND disconnected = 0 ORDER BY name_key""",
                (workspace,),
            )]

    def add_agent(self, workspace, name, provider=""):
        with self.session(write=True) as db:
            return self.insert_agent(db, workspace, name, provider)

    def insert_agent(self, db, workspace, name, provider):
        name = self.clean_name(name)
        if provider and provider not in AGENT_PROVIDERS:
            raise StoreError("Choose a supported agent type.")
        agent_id = str(uuid4())
        count = db.execute(
            "SELECT count(*) FROM agents WHERE workspace = ? AND disconnected = 0",
            (workspace,),
        ).fetchone()[0]
        if count >= 10:
            raise StoreError("This workspace already has 10 active agents.")
        try:
            db.execute(
                "INSERT INTO agents (id, workspace, name, name_key, provider) VALUES (?, ?, ?, ?, ?)",
                (agent_id, workspace, name, name.casefold(), provider),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreError("An active agent already has that name.") from exc
        return agent_id

    @staticmethod
    def clean_name(name):
        name = name.strip()
        if not 1 <= len(name) <= 60 or not name.isprintable():
            raise StoreError("Enter a name between 1 and 60 printable characters.")
        return name

    def rename(self, workspace, agent_id, name):
        name = self.clean_name(name)
        with self.session(write=True) as db:
            self.active_agent(db, workspace, agent_id)
            try:
                db.execute("UPDATE agents SET name = ?, name_key = ? WHERE id = ?",
                           (name, name.casefold(), agent_id))
            except sqlite3.IntegrityError as exc:
                raise StoreError("An active agent already has that name.") from exc

    def disconnect(self, workspace, agent_id):
        with self.session(write=True) as db:
            self.active_agent(db, workspace, agent_id)
            db.execute("UPDATE agents SET disconnected = 1, token_hash = NULL WHERE id = ?",
                       (agent_id,))
            db.execute("DELETE FROM oauth_grants WHERE agent_id = ?", (agent_id,))

    def issue_token(self, workspace, name):
        token = secrets.token_urlsafe(32)
        with self.session(write=True) as db:
            result = db.execute(
                """UPDATE agents SET token_hash = ?
                   WHERE workspace = ? AND name_key = ? AND disconnected = 0""",
                (self.hash_token(token), workspace, self.clean_name(name).casefold()),
            )
            if result.rowcount != 1:
                raise StoreError("Add the agent on the Agents page first.")
        return token

    def issue_token_for_agent(self, workspace, agent_id):
        token = secrets.token_urlsafe(32)
        with self.session(write=True) as db:
            self.active_agent(db, workspace, agent_id)
            db.execute("UPDATE agents SET token_hash = ? WHERE id = ?", (self.hash_token(token), agent_id))
        return token

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def identity(self, token):
        with self.session() as db:
            row = db.execute(
                "SELECT id, workspace FROM agents WHERE token_hash = ? AND disconnected = 0",
                (self.hash_token(token),),
            ).fetchone()
        return tuple(row) if row else None

    @staticmethod
    def check_text(text):
        if not text.strip() or len(text.encode()) > 16 * 1024:
            raise StoreError("Message text must contain between 1 and 16 KiB of content.")

    @staticmethod
    def check_quota(db, workspace, sender):
        recent, today = db.execute(
            """SELECT count(CASE WHEN sender = ? AND created >
                         strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 minute') THEN 1 END), count(*)
               FROM messages WHERE workspace = ? AND created >
                         strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')""",
            (sender, workspace),
        ).fetchone()
        if recent >= 30 or today >= 1000:
            raise StoreError("Message limit reached. Try again later.")

    def send(self, workspace, sender, recipient, text, request_key):
        self.check_text(text)
        if not 1 <= len(request_key) <= 100:
            raise StoreError("Request ID must contain 1 to 100 characters.")
        if sender == recipient:
            raise StoreError("Choose another agent as the recipient.")
        with self.session(write=True) as db:
            self.active_agent(db, workspace, sender)
            existing = db.execute(
                "SELECT id, recipient, body FROM messages WHERE sender = ? AND request_key = ?",
                (sender, request_key),
            ).fetchone()
            if existing:
                if (existing['recipient'], existing['body']) != (recipient, text):
                    raise StoreError("That request ID already belongs to another message.")
                return existing['id']
            target = self.active_agent(db, workspace, recipient)
            if target["token_hash"] is None and not db.execute(
                "SELECT 1 FROM oauth_grants WHERE agent_id = ? AND grant_expires > CAST(strftime('%s', 'now') AS INTEGER)",
                (recipient,),
            ).fetchone():
                raise StoreError("Recipient needs a connection.")
            self.check_quota(db, workspace, sender)
            message_id = str(uuid4())
            db.execute(
                """INSERT INTO messages (id, workspace, sender, recipient, body, request_key)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (message_id, workspace, sender, recipient, text, request_key),
            )
        return message_id

    def reply(self, workspace, sender, request_id, text):
        self.check_text(text)
        with self.session(write=True) as db:
            self.active_agent(db, workspace, sender)
            request = db.execute(
                """SELECT sender FROM messages WHERE id = ? AND workspace = ? AND recipient = ?
                   AND reply_to IS NULL AND created > strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')""",
                (request_id, workspace, sender),
            ).fetchone()
            if request is None:
                raise StoreError("Request is unavailable.")
            existing = db.execute(
                "SELECT id, body FROM messages WHERE reply_to = ?", (request_id,),
            ).fetchone()
            if existing:
                if existing['body'] != text:
                    raise StoreError("That request already has a reply.")
                return existing['id']
            self.active_agent(db, workspace, request['sender'])
            self.check_quota(db, workspace, sender)
            reply_id = str(uuid4())
            db.execute(
                """INSERT INTO messages (id, workspace, sender, recipient, body, reply_to)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (reply_id, workspace, sender, request['sender'], text, request_id),
            )
        return reply_id

    def inbox(self, workspace, recipient, *, message_id=None, cursor=None):
        with self.session(write=True) as db:
            self.active_agent(db, workspace, recipient)
            conditions = ["m.workspace = ?", "m.recipient = ?",
                          "m.created > strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')"]
            parameters = [workspace, recipient]
            if message_id is not None:
                conditions.append("m.id = ?")
                parameters.append(message_id)
            if cursor is not None:
                anchor = db.execute(
                    "SELECT created FROM messages WHERE workspace = ? AND recipient = ? AND id = ?",
                    (workspace, recipient, cursor),
                ).fetchone()
                if anchor is None:
                    raise StoreError("Inbox cursor is unavailable. Start at the first page.")
                conditions.append("(m.created, m.id) < (?, ?)")
                parameters.extend((anchor['created'], cursor))
            clause = " AND ".join(conditions)
            rows = db.execute(f"""
                SELECT m.id, m.sender AS sender_id, a.name AS sender, m.body AS text,
                       m.reply_to, m.created
                FROM messages m JOIN agents a ON a.id = m.sender
                WHERE {clause} ORDER BY m.created DESC, m.id DESC LIMIT 51
                """, parameters).fetchall()
            if message_id is not None and not rows:
                raise StoreError("Message is unavailable.")
            messages = [dict(row, type="reply" if row['reply_to'] else "request") for row in rows[:50]]
            db.executemany(
                "UPDATE messages SET fetched = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND fetched IS NULL", ((message['id'],) for message in messages),
            )
            return {"messages": messages, "next_cursor": messages[-1]['id'] if len(rows) > 50 else None}

    def activity(self, workspace):
        with self.session() as db:
            return [dict(row) for row in db.execute("""
                SELECT m.id, a.name AS sender, b.name AS recipient, m.body, m.created,
                       m.fetched, r.body AS reply, r.created AS replied_at
                FROM messages m JOIN agents a ON a.id = m.sender JOIN agents b ON b.id = m.recipient
                LEFT JOIN messages r ON r.reply_to = m.id
                WHERE m.workspace = ? AND m.reply_to IS NULL
                    AND m.created > strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
                ORDER BY coalesce(r.created, m.created) DESC, m.id DESC LIMIT 50
                """, (workspace,))]
