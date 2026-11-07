import sqlite3

DB_PATH = "gateway.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        public_key TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        target_amount REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_id TEXT,
        file_hash TEXT NOT NULL,
        filename TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tx_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_id TEXT UNIQUE NOT NULL,
        project_name TEXT,
        type TEXT,
        status TEXT DEFAULT 'pending'
    )
    """)

    conn.commit()
    conn.close()

def add_project(name, description, target_amount):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (name, description, target_amount) VALUES (?, ?, ?)",
        (name, description, target_amount)
    )
    conn.commit()
    conn.close()

def get_projects():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, target_amount FROM projects")
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "description": r[2], "target_amount": r[3]}
        for r in rows
    ]

if __name__ == "__main__":
    init_db()
    print("DB created")