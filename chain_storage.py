"""Task 8 - Person B: Save committed blocks to PostgreSQL, reload on startup."""
import json
import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tracechain:tracechain123@localhost:5432/tracechain")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_chain_db(db_url=None):
    # db_url is ignored, kept so node.py doesn't need to change
    # we use DATABASE_URL env var instead, one DB per node via different DB name in URL
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS blocks (
        idx INTEGER PRIMARY KEY,
        timestamp BIGINT NOT NULL,
        previous_hash TEXT NOT NULL,
        nonce BIGINT NOT NULL,
        hash TEXT NOT NULL,
        transactions_json TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def save_chain(db_url, chain):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM blocks")
    for block in chain:
        cur.execute(
            "INSERT INTO blocks (idx, timestamp, previous_hash, nonce, hash, transactions_json) VALUES (%s, %s, %s, %s, %s, %s)",
            (block["index"], block["timestamp"], block["previous_hash"], block["nonce"], block["hash"], json.dumps(block["transactions"]))
        )
    conn.commit()
    conn.close()

def load_chain(db_url):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT idx, timestamp, previous_hash, nonce, hash, transactions_json FROM blocks ORDER BY idx")
    rows = cur.fetchall()
    conn.close()
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


def init_bft_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bft_blocks (
            node TEXT NOT NULL,
            idx INTEGER NOT NULL,
            timestamp BIGINT NOT NULL,
            previous_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            transactions_json TEXT NOT NULL,
            commit_json TEXT NOT NULL,
            PRIMARY KEY (node, idx)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bft_engine (
            node TEXT PRIMARY KEY,
            snapshot_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT now()
        )""")
    conn.commit()
    cur.close()
    conn.close()


def _insert_bft_block(cur, node, b):
    cur.execute(
        """INSERT INTO bft_blocks (node, idx, timestamp, previous_hash, hash, transactions_json, commit_json)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (node, idx) DO NOTHING""",
        (node, b["index"], b["timestamp"], b["previous_hash"], b["hash"],
         json.dumps(b["transactions"]), json.dumps(b["commit"])))


def save_bft_block(node, block):
    conn = get_conn()
    cur = conn.cursor()
    _insert_bft_block(cur, node, block)
    conn.commit()
    cur.close()
    conn.close()


def replace_bft_chain(node, blocks):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM bft_blocks WHERE node = %s", (node,))
    for b in blocks:
        _insert_bft_block(cur, node, b)
    conn.commit()
    cur.close()
    conn.close()


def load_bft_chain(node):
    """Returns the blocks after genesis, in order."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""SELECT idx, timestamp, previous_hash, hash, transactions_json, commit_json
                   FROM bft_blocks WHERE node = %s ORDER BY idx""", (node,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"index": r[0], "timestamp": r[1], "transactions": json.loads(r[4]),
             "previous_hash": r[2], "hash": r[3], "commit": json.loads(r[5])} for r in rows]


def save_bft_engine(node, snapshot):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO bft_engine (node, snapshot_json, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (node) DO UPDATE SET snapshot_json = EXCLUDED.snapshot_json, updated_at = now()""",
        (node, json.dumps(snapshot)))
    conn.commit()
    cur.close()
    conn.close()


def load_bft_engine(node):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT snapshot_json FROM bft_engine WHERE node = %s", (node,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return json.loads(row[0]) if row else None