"""Task 8 - Person B: Save committed blocks to PostgreSQL, reload on startup.

Same functions as before. Three robustness changes (marked FIX):
  FIX 1  psycopg2 is imported only when the database is really used, so a node that keeps its ledger in files
         (no DATABASE_URL) starts even if psycopg2 is not installed.
  FIX 2  one connection per process, reopened if it dropped. A validator saves its voting state several times per block,
         and opening a new connection each time was slow (the consensus lock is held while it saves).
  FIX 3  creating the tables is safe when several nodes start at the same moment (docker compose starts them together):
         the node waits for the database, takes a PostgreSQL advisory lock, and retries.
"""
import contextlib
import json
import os
import threading
import time

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tracechain:tracechain123@localhost:5432/tracechain")

_conn = None
_lock = threading.RLock()


def _connect():
    import psycopg2                                        # FIX 1: imported here, not at the top of the file
    return psycopg2.connect(DATABASE_URL)


def get_conn():
    """A new connection (the caller closes it). Kept for code that still uses it."""
    return _connect()


@contextlib.contextmanager
def _cursor():
    """FIX 2: the shared connection, committed when the block succeeds and rolled back when it fails."""
    global _conn
    with _lock:
        if _conn is None or _conn.closed:
            _conn = _connect()
        cur = _conn.cursor()
        try:
            yield cur
            _conn.commit()
        except BaseException:
            try:
                _conn.rollback()
            except Exception:
                pass
            raise
        finally:
            cur.close()


def wait_for_database(seconds=30):
    """FIX 3: the database container may need a few seconds to be ready."""
    end = time.time() + seconds
    while True:
        try:
            _connect().close()
            return
        except ImportError:
            raise
        except Exception:
            if time.time() > end:
                raise
            time.sleep(1)


def _create_tables(statements):
    """FIX 3: one node creates the tables at a time (advisory lock), and a failure is retried."""
    wait_for_database()
    last = None
    for attempt in range(5):
        try:
            with _cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(7243001)")
                for statement in statements:
                    cur.execute(statement)
            return
        except Exception as error:
            last = error
            time.sleep(0.3 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- Proof of Work chain (old)

def init_chain_db(db_url=None):
    # db_url is ignored, kept so node.py doesn't need to change
    # we use DATABASE_URL env var instead, one DB per node via different DB name in URL
    _create_tables(["""
    CREATE TABLE IF NOT EXISTS blocks (
        idx INTEGER PRIMARY KEY,
        timestamp BIGINT NOT NULL,
        previous_hash TEXT NOT NULL,
        nonce BIGINT NOT NULL,
        hash TEXT NOT NULL,
        transactions_json TEXT NOT NULL
    )
    """])


def save_chain(db_url, chain):
    with _cursor() as cur:
        cur.execute("DELETE FROM blocks")
        for block in chain:
            cur.execute(
                "INSERT INTO blocks (idx, timestamp, previous_hash, nonce, hash, transactions_json) VALUES (%s, %s, %s, %s, %s, %s)",
                (block["index"], block["timestamp"], block["previous_hash"], block["nonce"], block["hash"], json.dumps(block["transactions"]))
            )


def load_chain(db_url):
    with _cursor() as cur:
        cur.execute("SELECT idx, timestamp, previous_hash, nonce, hash, transactions_json FROM blocks ORDER BY idx")
        rows = cur.fetchall()
    chain = []
    for r in rows:
        chain.append({
            "index": r[0],
            "timestamp": r[1],
            "previous_hash": r[2],
            "nonce": r[3],
            "hash": r[4],
            "transactions": json.loads(r[5])
        })
    return chain


# ---------------------------------------------------------------- BFT chain (current)

def init_bft_db():
    _create_tables(["""
        CREATE TABLE IF NOT EXISTS bft_blocks (
            node TEXT NOT NULL,
            idx INTEGER NOT NULL,
            timestamp BIGINT NOT NULL,
            previous_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            transactions_json TEXT NOT NULL,
            commit_json TEXT NOT NULL,
            PRIMARY KEY (node, idx)
        )""", """
        CREATE TABLE IF NOT EXISTS bft_engine (
            node TEXT PRIMARY KEY,
            snapshot_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT now()
        )"""])


def _insert_bft_block(cur, node, b):
    cur.execute(
        """INSERT INTO bft_blocks (node, idx, timestamp, previous_hash, hash, transactions_json, commit_json)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (node, idx) DO NOTHING""",
        (node, b["index"], b["timestamp"], b["previous_hash"], b["hash"],
         json.dumps(b["transactions"]), json.dumps(b["commit"])))


def save_bft_block(node, block):
    with _cursor() as cur:
        _insert_bft_block(cur, node, block)


def replace_bft_chain(node, blocks):
    with _cursor() as cur:
        cur.execute("DELETE FROM bft_blocks WHERE node = %s", (node,))
        for b in blocks:
            _insert_bft_block(cur, node, b)


def load_bft_chain(node):
    """Returns the blocks after genesis, in order."""
    with _cursor() as cur:
        cur.execute("""SELECT idx, timestamp, previous_hash, hash, transactions_json, commit_json
                       FROM bft_blocks WHERE node = %s ORDER BY idx""", (node,))
        rows = cur.fetchall()
    return [{"index": r[0], "timestamp": r[1], "transactions": json.loads(r[4]),
             "previous_hash": r[2], "hash": r[3], "commit": json.loads(r[5])} for r in rows]


def save_bft_engine(node, snapshot):
    with _cursor() as cur:
        cur.execute(
            """INSERT INTO bft_engine (node, snapshot_json, updated_at) VALUES (%s, %s, now())
               ON CONFLICT (node) DO UPDATE SET snapshot_json = EXCLUDED.snapshot_json, updated_at = now()""",
            (node, json.dumps(snapshot)))


def load_bft_engine(node):
    with _cursor() as cur:
        cur.execute("SELECT snapshot_json FROM bft_engine WHERE node = %s", (node,))
        row = cur.fetchone()
    return json.loads(row[0]) if row else None