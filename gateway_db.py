"""The gateway's database: the application data that does NOT belong on the blockchain.

    users            login, role, public key, private key (encrypted: the gateway signs for its users)
    charities        profile of a charity and whether an administrator verified it
    campaigns        what a charity collects money for
    tokens           login sessions (only a hash of the token is stored)
    transactions     what every user did (pending / confirmed / expired) and the block that holds it: a read model of the chain
    receipts         name, size and type of each uploaded receipt (the file itself is in the receipt service)
    meta             small settings

The blockchain stays the only source of truth for money: everything in `transactions` can be rebuilt from the chain.

One small SQL subset works on SQLite and PostgreSQL: TEXT, BIGINT, INTEGER (0/1 for booleans), epoch seconds for times,
ids created by the program, "ON CONFLICT ... DO UPDATE/NOTHING". Queries use :name parameters.
    sqlite:///gateway.db                                  (tested; a file)
    postgresql://user:password@host:5432/database         (needs psycopg2: the same SQL, not tested in the lab environment)
"""
import contextlib
import re
import sqlite3
import threading

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        public_key TEXT NOT NULL UNIQUE,
        private_key_enc TEXT NOT NULL,
        created_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS charities (
        user_id TEXT PRIMARY KEY REFERENCES users(id),
        description TEXT NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        verified_at BIGINT)""",
    """CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        charity_id TEXT NOT NULL REFERENCES users(id),
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        goal BIGINT,
        created_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tokens (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS transactions (
        tx_id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        sender TEXT NOT NULL,
        parent TEXT NOT NULL,
        amount BIGINT NOT NULL,
        memo TEXT NOT NULL,
        user_id TEXT,
        status TEXT NOT NULL,
        block_height BIGINT,
        commit_round INTEGER,
        note TEXT,
        created_at BIGINT NOT NULL,
        confirmed_at BIGINT)""",
    "CREATE INDEX IF NOT EXISTS transactions_user ON transactions (user_id)",
    "CREATE INDEX IF NOT EXISTS transactions_status ON transactions (status)",
    """CREATE TABLE IF NOT EXISTS receipts (
        hash TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        size BIGINT NOT NULL,
        content_type TEXT NOT NULL,
        uploader_id TEXT,
        uploaded_at BIGINT NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]


def postgres_sql(sql: str) -> str:
    """Turn  :name  parameters into  %(name)s  for psycopg2 (and protect any literal percent sign)."""
    return re.sub(r"(?<![:\w]):(\w+)", r"%(\1)s", sql.replace("%", "%%"))


class Database:
    def __init__(self, url: str):
        self.url = url
        self.lock = threading.RLock()
        if url.startswith("sqlite:///"):
            self.kind = "sqlite"
            self.conn = sqlite3.connect(url[len("sqlite:///"):] or ":memory:", check_same_thread=False, isolation_level=None)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
        elif url.startswith(("postgresql://", "postgres://")):
            import psycopg2                                          # only needed for PostgreSQL
            self.kind = "postgres"
            self.conn = psycopg2.connect(url)
            self.conn.autocommit = True
        else:
            raise ValueError("DATABASE_URL must start with sqlite:/// or postgresql://")
        for statement in SCHEMA:
            self.execute(statement)

    def execute(self, sql: str, params: dict = None) -> list:
        """Run one statement. Returns the rows (as dicts) of a SELECT or of a statement with RETURNING."""
        with self.lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(postgres_sql(sql) if self.kind == "postgres" else sql, params or {})
                if cursor.description is None:
                    return []
                names = [d[0] for d in cursor.description]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def one(self, sql: str, params: dict = None):
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    @contextlib.contextmanager
    def transaction(self):
        """Everything inside happens or nothing does."""
        with self.lock:
            if self.kind == "sqlite":
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    yield
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise
            else:
                self.conn.autocommit = False
                try:
                    yield
                    self.conn.commit()
                except BaseException:
                    self.conn.rollback()
                    raise
                finally:
                    self.conn.autocommit = True