import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tracechain:tracechain123@localhost:5432/tracechain")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, name TEXT, public_key TEXT UNIQUE, private_key_encrypted TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, name TEXT, description TEXT, target_amount INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS receipts (id SERIAL PRIMARY KEY, expense_id TEXT, hash TEXT, file_path TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS tx_index (tx_id TEXT PRIMARY KEY, block_index INTEGER)")
    conn.commit()
    conn.close()

def add_project(name, description, target_amount):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO projects (name, description, target_amount) VALUES (%s, %s, %s) RETURNING id", (name, description, target_amount))
    new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id

def get_projects():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, target_amount FROM projects")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "description": r[2], "target_amount": r[3]} for r in rows]