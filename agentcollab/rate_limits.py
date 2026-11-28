"""Persistent request budgets for the staging pilot."""

from time import time

WINDOW_SECONDS = 60
ACCOUNT_REQUESTS_PER_PEER = 20
ACCOUNT_REQUESTS_TOTAL = 60
INBOX_REQUESTS_PER_AGENT = 60
INBOX_REQUESTS_TOTAL = 300


class RequestLimits:
    def __init__(self, store):
        self.store = store
        with store.session(write=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS request_limits (
                    window INTEGER NOT NULL, scope TEXT NOT NULL, caller TEXT NOT NULL,
                    used INTEGER NOT NULL, PRIMARY KEY (window, scope, caller)
                )
            """)

    def consume(self, scope, caller, per_caller, total):
        """Spend from both limits together, or return the retry delay in seconds."""
        with self.store.session(write=True) as db:
            now = int(time())
            window = now - now % WINDOW_SECONDS
            db.execute("DELETE FROM request_limits WHERE window < ?", (window,))
            buckets = [("", total), (caller, per_caller)]
            for key, limit in buckets:
                row = db.execute(
                    "SELECT used FROM request_limits WHERE window = ? AND scope = ? AND caller = ?",
                    (window, scope, key),
                ).fetchone()
                if row and row["used"] >= limit:
                    return window + WINDOW_SECONDS - now
            db.executemany("""
                INSERT INTO request_limits (window, scope, caller, used) VALUES (?, ?, ?, 1)
                ON CONFLICT(window, scope, caller) DO UPDATE SET used = used + 1
            """, [(window, scope, key) for key, _ in buckets])
        return 0
