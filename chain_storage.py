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