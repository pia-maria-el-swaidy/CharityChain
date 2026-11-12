"""A stand-in for psycopg2 used ONLY by the tests: it runs the same SQL on SQLite, so that the PostgreSQL code paths
of chain_storage.py can be exercised where no PostgreSQL server exists. It knows just what the project uses:
  %s parameters, now(), ON CONFLICT ... EXCLUDED, pg_advisory_xact_lock, connection.closed.
Use with  DATABASE_URL=fakepg:///path/to/file.db  and  PYTHONPATH=test_support."""
import sqlite3


class Error(Exception):
    pass


class OperationalError(Error):
    pass


class _Cursor:
    def __init__(self, conn):
        self.conn, self.cur = conn, conn.raw.cursor()

    @property
    def description(self):
        return self.cur.description

    def execute(self, sql, params=()):
        if "pg_advisory_xact_lock" in sql:
            sql = "SELECT 1"
        sql = sql.replace("%s", "?").replace("now()", "CURRENT_TIMESTAMP")
        try:
            self.cur.execute(sql, params)
        except sqlite3.Error as error:
            raise Error(str(error))

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def close(self):
        self.cur.close()


class _Connection:
    def __init__(self, path):
        self.raw = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.raw.execute("PRAGMA journal_mode = WAL")
        self.closed = 0
        self.autocommit = False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()
        self.closed = 1


def connect(url):
    if not url.startswith("fakepg://"):
        raise OperationalError("could not connect to server (the test stand-in only knows fakepg://)")
    return _Connection(url[len("fakepg://"):])