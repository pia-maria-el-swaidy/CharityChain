import os
from contextlib import contextmanager
from gateway import keys
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]  # no hardcoded default


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
-- ===== GATEWAY DATA (names + status index only). The chain is the source of truth. =====
CREATE TABLE IF NOT EXISTS donors (
    donor_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT,
    email TEXT UNIQUE,
    is_anonymous BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (is_anonymous = TRUE OR email IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS charities (
    charity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    wallet_ref TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    charity_id UUID NOT NULL REFERENCES charities(charity_id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    goal_amount BIGINT CHECK (goal_amount > 0),
    currency CHAR(3) NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CLOSED','CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id TEXT PRIMARY KEY,                    -- same string as the "id" of the tx on the chain
    tx_type TEXT NOT NULL CHECK (tx_type IN ('DONATION','ALLOCATION','DISBURSEMENT')),
    donor_id UUID REFERENCES donors(donor_id) ON DELETE RESTRICT,
    charity_id UUID NOT NULL REFERENCES charities(charity_id) ON DELETE RESTRICT,
    campaign_id UUID REFERENCES campaigns(campaign_id) ON DELETE RESTRICT,
    recipient_ref TEXT,
    receipt_hash CHAR(64),
    amount BIGINT NOT NULL CHECK (amount > 0), -- minor units (cents)
    currency CHAR(3) NOT NULL,
    memo TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    tx_hash CHAR(64) NOT NULL UNIQUE,          -- gateway-only fingerprint, the chain does not use it
    signature TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','COMMITTED','REJECTED')),
    block_index BIGINT,                        -- plain number, NO foreign key: the chain is not in our tables
    tx_position INT,
    UNIQUE (block_index, tx_position),
    CHECK (tx_type <> 'DONATION' OR donor_id IS NOT NULL),
    CHECK (tx_type <> 'DISBURSEMENT' OR recipient_ref IS NOT NULL),
    CHECK (status <> 'PENDING' OR (block_index IS NULL AND tx_position IS NULL)),
    CHECK (status <> 'COMMITTED' OR (block_index IS NOT NULL AND tx_position IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS chain_sync (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    synced_height BIGINT NOT NULL DEFAULT 0    -- next block index the gateway has not read yet
);
INSERT INTO chain_sync (id, synced_height) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_tx_campaign ON transactions(campaign_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_index);
CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);

ALTER TABLE donors ADD COLUMN IF NOT EXISTS public_key TEXT UNIQUE;
ALTER TABLE donors ADD COLUMN IF NOT EXISTS private_key_encrypted TEXT;
ALTER TABLE charities ADD COLUMN IF NOT EXISTS public_key TEXT UNIQUE;
ALTER TABLE charities ADD COLUMN IF NOT EXISTS private_key_encrypted TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS parent_tx_id TEXT;  -- ALLOCATION -> donation tx_id, DISBURSEMENT -> allocation tx_id
"""

TX_COLUMNS = ("tx_id", "tx_type", "donor_id", "charity_id", "campaign_id",
              "recipient_ref", "receipt_hash", "amount", "currency", "memo",
              "created_at", "tx_hash", "signature", "parent_tx_id")


def add_transaction(tx: dict):
    """Save a PENDING transaction. tx_id, created_at and tx_hash are set by the caller."""
    row = {k: tx.get(k) for k in TX_COLUMNS}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO transactions (tx_id, tx_type, donor_id, charity_id, campaign_id, "
                "recipient_ref, receipt_hash, amount, currency, memo, created_at, tx_hash, signature, parent_tx_id) "
                "VALUES (%(tx_id)s, %(tx_type)s, %(donor_id)s, %(charity_id)s, %(campaign_id)s, "
                "%(recipient_ref)s, %(receipt_hash)s, %(amount)s, %(currency)s, %(memo)s, "
                "%(created_at)s, %(tx_hash)s, %(signature)s, %(parent_tx_id)s)",
                row,
            )

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)


# ---------- campaigns ----------
def add_campaign(charity_id, title, goal_amount, currency="USD"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO campaigns (charity_id, title, goal_amount, currency) "
                "VALUES (%s, %s, %s, %s) RETURNING campaign_id",
                (charity_id, title, goal_amount, currency),
            )
            return cur.fetchone()["campaign_id"]


def get_campaigns():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT campaign_id, charity_id, title, goal_amount, currency, status "
                        "FROM campaigns ORDER BY created_at DESC")
            return cur.fetchall()


def get_transaction(tx_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT t.*, d.is_anonymous FROM transactions t "
                        "LEFT JOIN donors d ON d.donor_id = t.donor_id WHERE t.tx_id = %s", (tx_id,))
            return cur.fetchone()


def get_pending_transactions(limit=100):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM transactions WHERE status = 'PENDING' "
                        "ORDER BY created_at, tx_id LIMIT %s", (limit,))
            return cur.fetchall()


def get_block_transactions(block_index):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM transactions WHERE block_index = %s "
                        "ORDER BY tx_position", (block_index,))
            return cur.fetchall()

# ---------- transactions: HTTP-only integration with the chain ----------
def create_transaction(tx: dict, submit):
    """Save the row as PENDING, then call submit(tx) to send it to the chain.
    If submit() raises (chain down or tx rejected), the row is deleted and the error is re-raised.
    The row is committed BEFORE submit(), so the chain can never commit a block
    that contains a transaction we have not stored yet."""
    add_transaction(tx)
    try:
        return submit(tx)
    except Exception:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM transactions WHERE tx_id = %s AND status = 'PENDING'",
                            (tx["tx_id"],))
        raise


def mark_committed(tx_ids, block_index):
    """tx_ids = ALL transaction ids of the block, in block order (position = index in the list).
    Updates only our PENDING rows. Ids we do not know are ignored. Safe to run twice."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            for pos, tx_id in enumerate(tx_ids):
                if tx_id is None:
                    continue
                cur.execute(
                    "UPDATE transactions SET status = 'COMMITTED', block_index = %s, tx_position = %s "
                    "WHERE tx_id = %s AND status = 'PENDING'",
                    (block_index, pos, str(tx_id)),
                )


# ---------- chain_sync ----------
def get_synced_height():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT synced_height FROM chain_sync WHERE id = 1")
            return cur.fetchone()["synced_height"]


def set_synced_height(height):
    """Only ever moves forward."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE chain_sync SET synced_height = GREATEST(synced_height, %s) WHERE id = 1",
                        (height,))

def add_donor(display_name, email, is_anonymous=False):
    private_hex, public_hex = keys.generate_keypair()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO donors (display_name, email, is_anonymous, public_key, private_key_encrypted) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING donor_id",
                (display_name, email, is_anonymous, public_hex, keys.encrypt_private(private_hex)),
            )
            return cur.fetchone()["donor_id"]


def add_charity(name, description, wallet_ref):
    private_hex, public_hex = keys.generate_keypair()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO charities (name, description, wallet_ref, public_key, private_key_encrypted) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING charity_id",
                (name, description, wallet_ref, public_hex, keys.encrypt_private(private_hex)),
            )
            return cur.fetchone()["charity_id"]


_WALLET_ID_COLUMN = {"donors": "donor_id", "charities": "charity_id"}   # whitelist, never user input


def ensure_wallet(table, row_id):
    """Return (public_hex, private_hex) for a donor or charity. Creates the keypair on first use,
    so rows made before wallets existed get one automatically."""
    id_col = _WALLET_ID_COLUMN[table]
    select = f"SELECT public_key, private_key_encrypted FROM {table} WHERE {id_col} = %s"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(select, (str(row_id),))
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"unknown {table} id {row_id}")
            if row["public_key"]:
                return row["public_key"], keys.decrypt_private(row["private_key_encrypted"])
            private_hex, public_hex = keys.generate_keypair()
            cur.execute(
                f"UPDATE {table} SET public_key = %s, private_key_encrypted = %s "
                f"WHERE {id_col} = %s AND public_key IS NULL",
                (public_hex, keys.encrypt_private(private_hex), str(row_id)),
            )
            if cur.rowcount == 1:
                return public_hex, private_hex
            cur.execute(select, (str(row_id),))          # someone created it at the same moment
            row = cur.fetchone()
            return row["public_key"], keys.decrypt_private(row["private_key_encrypted"])

def get_charities():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT charity_id, name, description, wallet_ref FROM charities "
                        "ORDER BY created_at DESC")
            return cur.fetchall()

def list_transactions(campaign_id=None, status=None, limit=50, offset=0):
    where, params = [], []
    if campaign_id:
        where.append("t.campaign_id = %s"); params.append(campaign_id)
    if status:
        where.append("t.status = %s"); params.append(status)
    sql = ("SELECT t.*, d.is_anonymous FROM transactions t "
           "LEFT JOIN donors d ON d.donor_id = t.donor_id "
           + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "ORDER BY t.created_at DESC LIMIT %s OFFSET %s")
    params += [limit, offset]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()