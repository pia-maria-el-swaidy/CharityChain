"""TraceChain API gateway: the one door for the web frontend.

    users (donors, charities, an administrator)  --HTTP/JSON-->  gateway  --signed transactions-->  validators
                                                                    |  \\--reads, verified by itself--> any node (light client)
                                                                    |----> database   (accounts, directory, campaigns, history)
                                                                    \\----> receipt service   (the receipt files)

What the gateway does
    accounts     register / login (password hash, random session tokens). Roles: donor, charity, admin.
    keys         every account gets an ed25519 key pair; the private key is stored ENCRYPTED and the gateway signs the
                 account's transactions for it (a "custodial wallet": simple, but the gateway is trusted with the keys).
    directory    which public key is which charity; an administrator must verify a charity before it can receive donations.
    writes       POST /api/donations, /api/allocations, /api/expenses: build, sign and submit the transaction.
                 The chain's rules (R1-R4) are the judge: an overspending is refused by the nodes, with the reason.
    reads        everything about money comes from the light client (lightclient.py): the gateway checks every block's
                 commit certificate and every rule itself, so a lying node cannot make it show a wrong figure.
    receipts     the file goes to the receipt service, its SHA-256 hash goes on the chain in the expense.

Settings (environment variables or options)
    PORT, HOST            where the gateway listens (default 8000)
    DATABASE_URL          sqlite:///gateway.db (default)  or  postgresql://user:password@host:5432/database
    GATEWAY_DATA_DIR      folder for the encryption key of the wallets (default gateway_data)
    GATEWAY_MASTER_KEY    the key that encrypts the wallets (default: created in GATEWAY_DATA_DIR on the first start)
    VALIDATORS            public keys of the validators, comma separated (to verify certificates)
    NODE_URLS             nodes that receive the transactions (the validators)
    CHAIN_URLS            nodes the chain is read from (default: NODE_URLS); any node will do, none is trusted
    RECEIPTS_URL, RECEIPT_API_KEY
    ADMIN_EMAIL, ADMIN_PASSWORD   creates the first administrator (a random password is printed if none is given)
    INDEX_INTERVAL        seconds between two readings of the chain (default 1)
"""
import argparse
import functools
import hashlib
import os
import re
import secrets
import sys
import threading
import time
import types

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, Response, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

import bft
import wallet
from gateway_db import Database
from lightclient import LightClient

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

cfg = types.SimpleNamespace(node_urls=[], chain_urls=[], receipts_url="", receipts_key="", token_hours=12,
                            index_interval=1.0, pending_timeout=120, max_receipt=5 * 1024 * 1024)
db = None
light = None
vault = None

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
MAX_AMOUNT = 10 ** 12


# ---------------------------------------------------------------- small helpers

def now():
    return int(time.time())


def fail(status, message, **extra):
    return jsonify({"error": message, **extra}), status


def get_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_AMOUNT else None


def clean_text(value, limit):
    """A short single-line text without control characters, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit and all(c.isprintable() for c in value) else None


def public_user(u):
    return {"id": u["id"], "name": u["name"], "role": u["role"], "public_key": u["public_key"]}


class Vault:
    """Encrypts the private keys of the accounts (Fernet: AES with an authentication tag)."""

    def __init__(self, key):
        self.fernet = Fernet(key)

    def encrypt(self, text):
        return self.fernet.encrypt(text.encode()).decode()

    def decrypt(self, token):
        return self.fernet.decrypt(token.encode()).decode()


def load_master_key(data_dir):
    key = os.environ.get("GATEWAY_MASTER_KEY")
    if key:
        return key.encode()
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "master.key")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(key)
    print(f"created the wallet encryption key {path}: keep it safe, without it the accounts' keys cannot be opened")
    return key


# ---------------------------------------------------------------- authentication

failures = {}                                       # email -> (number of failures, locked until)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_user():
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not token:
        return None
    return db.one("""SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id
                     WHERE t.token_hash = :h AND t.expires_at > :now""", {"h": token_hash(token), "now": now()})


def require(*roles):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return fail(401, "log in first: send the header  Authorization: Bearer <token>")
            if roles and user["role"] not in roles:
                return fail(403, f"this needs the role {' or '.join(roles)}, you are a {user['role']}")
            g.user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def create_user(email, name, role, password, description=""):
    private, public = wallet.generate_keypair()
    user = {"id": secrets.token_hex(8), "email": email, "name": name, "role": role,
            "password_hash": generate_password_hash(password), "public_key": public,
            "private_key_enc": vault.encrypt(private), "created_at": now()}
    with db.transaction():
        db.execute("""INSERT INTO users (id, email, name, role, password_hash, public_key, private_key_enc, created_at)
                      VALUES (:id, :email, :name, :role, :password_hash, :public_key, :private_key_enc, :created_at)""", user)
        if role == "charity":
            db.execute("INSERT INTO charities (user_id, description, verified) VALUES (:id, :d, 0)",
                       {"id": user["id"], "d": description})
    return user


@app.post("/api/auth/register")
def register():
    b = get_body()
    if b is None:
        return fail(400, "the body must be a JSON object")
    email = b.get("email").strip().lower() if isinstance(b.get("email"), str) else ""
    name, password, role = clean_text(b.get("name"), 80), b.get("password"), b.get("role")
    if not EMAIL.match(email) or len(email) > 120:
        return fail(400, "a valid email address is needed")
    if name is None:
        return fail(400, "a name of 1 to 80 characters is needed")
    if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
        return fail(400, "the password must have at least 8 characters")
    if role not in ("donor", "charity"):
        return fail(400, "role must be donor or charity")
    description = ""
    if role == "charity":
        description = clean_text(b.get("description"), 1000)
        if description is None:
            return fail(400, "a charity needs a description (1 to 1000 characters)")
    if db.one("SELECT id FROM users WHERE email = :e", {"e": email}):
        return fail(409, "this email is already registered")
    try:
        user = create_user(email, name, role, password, description)
    except Exception:                                    # two registrations at the same moment
        return fail(409, "this email is already registered")
    note = "an administrator must verify your charity before it can receive donations" if role == "charity" else None
    return jsonify({"user": public_user(user), "note": note}), 201


@app.post("/api/auth/login")
def login():
    b = get_body() or {}
    email = b.get("email").strip().lower() if isinstance(b.get("email"), str) else ""
    count, until = failures.get(email, (0, 0))
    if until > time.time():
        return fail(429, "too many wrong passwords: wait a minute")
    user = db.one("SELECT * FROM users WHERE email = :e", {"e": email})
    if user is None or not isinstance(b.get("password"), str) or not check_password_hash(user["password_hash"], b["password"]):
        count += 1
        failures[email] = (count, time.time() + 60 if count >= 5 else 0)
        return fail(401, "wrong email or password")
    failures.pop(email, None)
    token = secrets.token_urlsafe(32)
    expires = now() + cfg.token_hours * 3600
    db.execute("INSERT INTO tokens (token_hash, user_id, created_at, expires_at) VALUES (:h, :u, :c, :e)",
               {"h": token_hash(token), "u": user["id"], "c": now(), "e": expires})
    return jsonify({"token": token, "expires_at": expires, "user": dict(public_user(user), email=user["email"])})


@app.post("/api/auth/logout")
@require()
def logout():
    header = request.headers.get("Authorization", "")
    db.execute("DELETE FROM tokens WHERE token_hash = :h", {"h": token_hash(header[7:].strip())})
    return jsonify({"message": "logged out"})


@app.get("/api/me")
@require()
def me():
    u = g.user
    out = dict(public_user(u), email=u["email"])
    if u["role"] == "charity":
        c = db.one("SELECT description, verified FROM charities WHERE user_id = :i", {"i": u["id"]})
        out.update(description=c["description"], verified=bool(c["verified"]), totals=light.totals(u["public_key"]))
    return jsonify(out)


# ---------------------------------------------------------------- directory: charities and campaigns

def charity_card(row, with_campaigns=False):
    card = {"id": row["id"], "name": row["name"], "description": row["description"], "public_key": row["public_key"],
            "verified": bool(row["verified"]), "totals": light.totals(row["public_key"])}
    if with_campaigns:
        raised = light.raised(row["public_key"])
        card["campaigns"] = [{"slug": c["slug"], "title": c["title"], "description": c["description"], "goal": c["goal"],
                              "raised": raised.get(c["slug"], 0)}
                             for c in db.execute("SELECT slug, title, description, goal FROM campaigns WHERE charity_id = :i "
                                                 "ORDER BY created_at", {"i": row["id"]})]
    return card


@app.get("/api/charities")
def list_charities():
    rows = db.execute("""SELECT u.id, u.name, u.public_key, c.description, c.verified FROM users u
                         JOIN charities c ON c.user_id = u.id WHERE c.verified = 1 ORDER BY u.name""")
    return jsonify({"charities": [charity_card(r) for r in rows]})


@app.get("/api/charities/<charity_id>")
def get_charity(charity_id):
    row = db.one("""SELECT u.id, u.name, u.public_key, c.description, c.verified FROM users u
                    JOIN charities c ON c.user_id = u.id WHERE u.id = :i AND c.verified = 1""", {"i": charity_id})
    if row is None:
        return fail(404, "unknown charity")
    names = {r["public_key"]: r["name"] for r in db.execute("SELECT public_key, name FROM users")}
    card = charity_card(row, with_campaigns=True)
    card["recent_donations"] = [dict(d, donor_name=names.get(d["donor"]), unallocated=d["amount"] - d["allocated"])
                                for d in light.donations(charity=row["public_key"])[:20]]
    return jsonify(card)


@app.post("/api/campaigns")
@require("charity")
def create_campaign():
    c = db.one("SELECT verified FROM charities WHERE user_id = :i", {"i": g.user["id"]})
    if not c["verified"]:
        return fail(403, "your charity has not been verified yet")
    b = get_body() or {}
    slug, title, description = b.get("slug"), clean_text(b.get("title"), 100), clean_text(b.get("description"), 1000)
    goal = b.get("goal")
    if not isinstance(slug, str) or not SLUG.match(slug):
        return fail(400, "slug: 1 to 40 characters, lowercase letters, digits and hyphens")
    if title is None or description is None:
        return fail(400, "a title (up to 100 characters) and a description (up to 1000) are needed")
    if goal is not None and positive_int(goal) is None:
        return fail(400, "goal must be a positive whole number")
    if db.one("SELECT id FROM campaigns WHERE slug = :s", {"s": slug}):
        return fail(409, "this slug is already used")
    db.execute("""INSERT INTO campaigns (id, charity_id, slug, title, description, goal, created_at)
                  VALUES (:id, :c, :s, :t, :d, :g, :n)""",
               {"id": secrets.token_hex(8), "c": g.user["id"], "s": slug, "t": title, "d": description, "g": goal, "n": now()})
    return jsonify({"slug": slug, "title": title}), 201


@app.get("/api/campaigns")
def list_campaigns():
    rows = db.execute("""SELECT k.slug, k.title, k.description, k.goal, u.id AS charity_id, u.name AS charity_name, u.public_key
                         FROM campaigns k JOIN users u ON u.id = k.charity_id JOIN charities c ON c.user_id = u.id
                         WHERE c.verified = 1 ORDER BY k.created_at""")
    out = []
    for r in rows:
        out.append({"slug": r["slug"], "title": r["title"], "description": r["description"], "goal": r["goal"],
                    "charity_id": r["charity_id"], "charity_name": r["charity_name"],
                    "raised": light.raised(r["public_key"]).get(r["slug"], 0)})
    return jsonify({"campaigns": out})


# ---------------------------------------------------------------- writing to the chain

def submit(tx):
    """Send a signed transaction to the nodes. A rule that says no is final; a node that does not answer is skipped."""
    last = "no node answered"
    for url in cfg.node_urls:
        try:
            r = requests.post(url + "/transactions/new", json=tx, timeout=5)
        except requests.RequestException:
            continue
        if r.status_code == 201:
            return 201, {}
        try:
            error = r.json().get("error", r.text)
        except ValueError:
            error = r.text
        if r.status_code in (400, 422):
            return 422, {"error": error}
        last = f"{url} answered {r.status_code}"
    return 503, {"error": f"the blockchain network is not reachable ({last})"}


def build_and_submit(user, kind, parent, amount, memo, note=None):
    tx = {"id": f"{kind[0].lower()}-{secrets.token_hex(5)}", "type": kind, "sender": user["public_key"],
          "parent": parent, "amount": amount, "memo": memo}
    try:
        signed = wallet.sign_transaction(tx, vault.decrypt(user["private_key_enc"]))
    except InvalidToken:
        return fail(500, "this account's key cannot be opened: the gateway's encryption key has changed")
    db.execute("""INSERT INTO transactions (tx_id, type, sender, parent, amount, memo, user_id, status, note, created_at)
                  VALUES (:id, :type, :sender, :parent, :amount, :memo, :u, 'pending', :note, :n)
                  ON CONFLICT (tx_id) DO NOTHING""", dict(tx, u=user["id"], note=note, n=now()))
    status, result = submit(signed)
    if status != 201:
        db.execute("DELETE FROM transactions WHERE tx_id = :i AND status = 'pending'", {"i": tx["id"]})
        return fail(status, result["error"])
    return jsonify({"tx_id": tx["id"], "status": "pending", "check": f"/api/transactions/{tx['id']}"}), 202


def verified_charity(user):
    c = db.one("SELECT verified FROM charities WHERE user_id = :i", {"i": user["id"]})
    return bool(c and c["verified"])


@app.post("/api/donations")
@require("donor")
def create_donation():
    b = get_body() or {}
    amount = positive_int(b.get("amount"))
    if amount is None:
        return fail(400, f"amount must be a whole number between 1 and {MAX_AMOUNT}")
    charity_id = b.get("charity_id")
    if not isinstance(charity_id, str):
        return fail(400, "charity_id is needed")
    charity = db.one("""SELECT u.id, u.public_key, c.verified FROM users u JOIN charities c ON c.user_id = u.id
                        WHERE u.id = :i""", {"i": charity_id})
    if charity is None:
        return fail(404, "unknown charity")
    if not charity["verified"]:
        return fail(409, "this charity has not been verified yet, it cannot receive donations")
    campaign = b.get("campaign") or ""
    if campaign:
        if not isinstance(campaign, str) or not SLUG.match(campaign) or \
                not db.one("SELECT id FROM campaigns WHERE slug = :s AND charity_id = :c", {"s": campaign, "c": charity_id}):
            return fail(400, "this charity has no such campaign")
    return build_and_submit(g.user, "DONATION", charity["public_key"], amount, campaign)


@app.post("/api/allocations")
@require("charity")
def create_allocation():
    if not verified_charity(g.user):
        return fail(403, "your charity has not been verified yet")
    b = get_body() or {}
    donation_id, project, amount = b.get("donation_id"), b.get("project"), positive_int(b.get("amount"))
    if not isinstance(donation_id, str) or not ITEM_ID.match(donation_id):
        return fail(400, "donation_id is needed")
    if not isinstance(project, str) or not SLUG.match(project):
        return fail(400, "project: 1 to 40 characters, lowercase letters, digits and hyphens")
    if amount is None:
        return fail(400, f"amount must be a whole number between 1 and {MAX_AMOUNT}")
    return build_and_submit(g.user, "ALLOCATION", donation_id, amount, project)


def sniff(data):
    """The kind of file by its first bytes (a receipt is a PDF, a PNG or a JPEG)."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def store_receipt(data):
    try:
        r = requests.post(cfg.receipts_url + "/receipts", data=data, timeout=10,
                          headers={"X-Api-Key": cfg.receipts_key, "Content-Type": "application/octet-stream"})
    except requests.RequestException:
        return None, "the receipt service is not reachable"
    if r.status_code not in (200, 201):
        return None, f"the receipt service refused the file ({r.status_code})"
    return r.json().get("hash"), None


@app.post("/api/expenses")
@require("charity")
def create_expense():
    if not verified_charity(g.user):
        return fail(403, "your charity has not been verified yet")
    allocation_id = request.form.get("allocation_id", "")
    if not ITEM_ID.match(allocation_id):
        return fail(400, "allocation_id is needed")
    try:
        amount = positive_int(int(request.form.get("amount", "")))
    except ValueError:
        amount = None
    if amount is None:
        return fail(400, f"amount must be a whole number between 1 and {MAX_AMOUNT}")
    description = clean_text(request.form.get("description"), 200)
    if description is None:
        return fail(400, "a description of the expense is needed (up to 200 characters)")
    upload = request.files.get("file")
    if upload is None:
        return fail(400, "the receipt is needed: send the form field  file")
    data = upload.read(cfg.max_receipt + 1)
    if len(data) > cfg.max_receipt:
        return fail(413, f"a receipt can be at most {cfg.max_receipt // (1024 * 1024)} MB")
    content_type = sniff(data)
    if content_type is None:
        return fail(415, "a receipt must be a PDF, a PNG or a JPEG file")
    digest = hashlib.sha256(data).hexdigest()                 # computed here: the service is not trusted either
    stored, problem = store_receipt(data)
    if problem:
        return fail(503, problem)
    if stored != digest:
        return fail(502, "the receipt service returned another hash: the file was not stored correctly")
    db.execute("""INSERT INTO receipts (hash, filename, size, content_type, uploader_id, uploaded_at)
                  VALUES (:h, :f, :s, :c, :u, :n) ON CONFLICT (hash) DO NOTHING""",
               {"h": digest, "f": (upload.filename or "receipt")[:100], "s": len(data), "c": content_type,
                "u": g.user["id"], "n": now()})
    return build_and_submit(g.user, "EXPENSE", allocation_id, amount, digest, note=description)


# ---------------------------------------------------------------- reading: transactions, traces, blocks, receipts

@app.get("/api/transactions/<tx_id>")
def get_transaction(tx_id):
    row = db.one("SELECT type, amount, status, note, created_at FROM transactions WHERE tx_id = :i", {"i": tx_id})
    location = light.location(tx_id)
    if location is not None:
        return jsonify({"tx_id": tx_id, "status": "confirmed", "final": True, "location": location,
                        "type": row["type"] if row else None, "amount": row["amount"] if row else None})
    if row is None:
        return fail(404, "unknown transaction")
    return jsonify({"tx_id": tx_id, "status": row["status"], "final": False, "type": row["type"], "amount": row["amount"],
                    "hint": "pending: waiting for the validators" if row["status"] == "pending"
                    else "expired: it did not reach a block, send it again"})


@app.get("/api/me/transactions")
@require()
def my_transactions():
    rows = db.execute("""SELECT tx_id, type, amount, memo, status, block_height, note, created_at, confirmed_at
                         FROM transactions WHERE user_id = :u ORDER BY created_at DESC LIMIT 200""", {"u": g.user["id"]})
    return jsonify({"transactions": rows})


def enrich_trace(trace):
    """Add names, receipts and block references to the verified trace."""
    names = {r["public_key"]: r["name"] for r in db.execute("SELECT public_key, name FROM users")}
    d = trace["donation"]
    d.update(donor_name=names.get(d["donor"]), charity_name=names.get(d["charity"]),
             campaign=light.campaign_of(d["id"]), location=light.location(d["id"]))
    for a in trace["allocations"]:
        a["location"] = light.location(a["id"])
        for e in a["expenses"]:
            e["location"] = light.location(e["id"])
            r = db.one("SELECT filename, size, content_type FROM receipts WHERE hash = :h", {"h": e["receipt_hash"]})
            e["receipt"] = {"hash": e["receipt_hash"], "filename": r["filename"] if r else None,
                            "size": r["size"] if r else None, "content_type": r["content_type"] if r else None,
                            "url": f"/api/receipts/{e['receipt_hash']}"}
            n = db.one("SELECT note FROM transactions WHERE tx_id = :i", {"i": e["id"]})
            e["description"] = n["note"] if n else None
    trace["verified"] = {"by": "this gateway, from the validators' commit certificates and the rules R1-R4",
                         "chain_height": light.height()}
    return trace


@app.get("/api/trace/<donation_id>")
def get_trace(donation_id):
    trace = light.trace(donation_id)
    if trace is None:
        return fail(404, "unknown donation")
    return jsonify(enrich_trace(trace))


@app.get("/api/blocks/<int:index>")
def get_block(index):
    block = light.block(index)
    if block is None:
        return fail(404, "no such block")
    signers = []
    if index > 0:
        signers = sorted(light.validators.index(s["validator"]) for s in block["commit"]["signatures"])
    return jsonify({"block": block, "verified": True, "signed_by_validators": signers,
                    "needed": bft.quorum(len(light.validators))})


def receipt_info(receipt_hash):
    """Is the stored file still the file with this hash? True / False / None (service not reachable)."""
    try:
        r = requests.get(f"{cfg.receipts_url}/receipts/{receipt_hash}/info", timeout=5)
    except requests.RequestException:
        return None
    return r.json().get("intact") if r.status_code == 200 else False


@app.get("/api/receipts/<receipt_hash>")
def download_receipt(receipt_hash):
    if not HASH.match(receipt_hash):
        return fail(400, "a receipt hash is 64 lowercase hex characters")
    if not light.expenses_with_receipt(receipt_hash):
        return fail(404, "this receipt is not referenced by any expense on the chain")
    try:
        r = requests.get(f"{cfg.receipts_url}/receipts/{receipt_hash}", timeout=10)
    except requests.RequestException:
        return fail(503, "the receipt service is not reachable")
    if r.status_code != 200:
        return fail(502, "the receipt service could not give the file back (it may be damaged or missing)")
    if hashlib.sha256(r.content).hexdigest() != receipt_hash:            # never forward a file that does not match
        return fail(502, "the stored file does not match its hash: it was changed")
    row = db.one("SELECT filename, content_type FROM receipts WHERE hash = :h", {"h": receipt_hash})
    name = (row["filename"] if row else receipt_hash[:12]).replace('"', "")
    return Response(r.content, mimetype=row["content_type"] if row else "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "X-Receipt-Hash": receipt_hash})


@app.get("/api/receipts/<receipt_hash>/verify")
def verify_receipt(receipt_hash):
    if not HASH.match(receipt_hash):
        return fail(400, "a receipt hash is 64 lowercase hex characters")
    expenses = light.expenses_with_receipt(receipt_hash)
    intact = receipt_info(receipt_hash) if expenses else None
    return jsonify({"hash": receipt_hash, "on_chain": bool(expenses), "expenses": expenses,
                    "file_matches_hash": intact,
                    "how_to_check_yourself": "download the file, compute its SHA-256 and compare it with this hash"})


# ---------------------------------------------------------------- administration

@app.get("/api/admin/charities")
@require("admin")
def admin_charities():
    rows = db.execute("""SELECT u.id, u.name, u.email, u.public_key, c.description, c.verified FROM users u
                         JOIN charities c ON c.user_id = u.id ORDER BY c.verified, u.created_at""")
    return jsonify({"charities": [dict(r, verified=bool(r["verified"])) for r in rows]})


@app.post("/api/admin/charities/<charity_id>/verify")
@require("admin")
def admin_verify(charity_id):
    if db.one("SELECT user_id FROM charities WHERE user_id = :i", {"i": charity_id}) is None:
        return fail(404, "unknown charity")
    db.execute("UPDATE charities SET verified = 1, verified_at = :n WHERE user_id = :i", {"n": now(), "i": charity_id})
    return jsonify({"message": "charity verified", "id": charity_id})


@app.post("/api/admin/reindex")
@require("admin")
def admin_reindex():
    """Forget what was read from the chain and read and verify everything again from the genesis block."""
    light.reset()
    added = light.sync()
    return jsonify({"verified_blocks": len(added), "height": light.height(), "error": light.error})


# ---------------------------------------------------------------- health, errors, CORS

def reachable(url, path="/status"):
    try:
        return requests.get(url + path, timeout=1).ok
    except requests.RequestException:
        return False


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "database": db.kind, "chain": light.status(),
                    "nodes": {u: reachable(u) for u in cfg.node_urls},
                    "receipt_service": reachable(cfg.receipts_url, "/health") if cfg.receipts_url else False})


@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.errorhandler(404)
def not_found(_):
    return fail(404, "not found")


@app.errorhandler(405)
def not_allowed(_):
    return fail(405, "method not allowed")


@app.errorhandler(413)
def too_big(_):
    return fail(413, "the request is too big")


@app.errorhandler(500)
def server_error(_):
    return fail(500, "internal error")


# ---------------------------------------------------------------- reading the chain in the background

def on_blocks(blocks):
    """New verified blocks: record in the database that the transactions are confirmed."""
    with db.transaction():
        for block in blocks:
            for t in block["transactions"]:
                owner = db.one("SELECT id FROM users WHERE public_key = :k", {"k": t["sender"]})
                db.execute("""INSERT INTO transactions (tx_id, type, sender, parent, amount, memo, user_id, status,
                                                       block_height, commit_round, created_at, confirmed_at)
                              VALUES (:id, :type, :sender, :parent, :amount, :memo, :u, 'confirmed', :h, :r, :t, :t)
                              ON CONFLICT (tx_id) DO UPDATE SET status = 'confirmed', block_height = excluded.block_height,
                                  commit_round = excluded.commit_round, confirmed_at = excluded.confirmed_at""",
                           {"id": t["id"], "type": t["type"], "sender": t["sender"], "parent": t["parent"],
                            "amount": t["amount"], "memo": t["memo"], "u": owner["id"] if owner else None,
                            "h": block["index"], "r": block["commit"]["round"], "t": block["timestamp"]})


def indexer_loop():
    while True:
        try:
            light.sync()
            db.execute("UPDATE transactions SET status = 'expired' WHERE status = 'pending' AND created_at < :c",
                       {"c": now() - cfg.pending_timeout})
        except Exception as error:                            # the loop must never die
            print("indexer error:", repr(error), file=sys.stderr)
        time.sleep(cfg.index_interval)


# ---------------------------------------------------------------- start

def split(text):
    return [u.strip().rstrip("/") for u in (text or "").split(",") if u.strip()]


def setup(args):
    global db, light, vault
    keys = [k.strip() for k in args.validators.split(",") if k.strip()]
    if not keys or any(not wallet.is_public_key(k) for k in keys) or len(set(keys)) != len(keys):
        sys.exit("VALIDATORS must list the validators' public keys (64 hex characters each), comma separated")
    cfg.node_urls = split(args.node_urls)
    cfg.chain_urls = split(args.chain_urls) or cfg.node_urls
    if not cfg.node_urls:
        sys.exit("NODE_URLS must list at least one node")
    cfg.receipts_url, cfg.receipts_key = args.receipts_url.rstrip("/"), args.receipts_key
    cfg.index_interval = args.index_interval
    db = Database(args.database_url)
    vault = Vault(load_master_key(args.data_dir))
    light = LightClient(keys, cfg.chain_urls, on_blocks=on_blocks)
    if args.admin_email:
        email = args.admin_email.strip().lower()
        if not db.one("SELECT id FROM users WHERE role = 'admin'") and not db.one("SELECT id FROM users WHERE email = :e", {"e": email}):
            password = args.admin_password or secrets.token_urlsafe(12)
            create_user(email, "Administrator", "admin", password)
            if not args.admin_password:
                print(f"administrator created: {email} / {password}   (shown once: write it down)")
            else:
                print(f"administrator created: {email}")


if __name__ == "__main__":
    env = os.environ.get
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(env("PORT", 8000)))
    parser.add_argument("--host", default=env("HOST", "127.0.0.1"), help="use 0.0.0.0 inside Docker")
    parser.add_argument("--database-url", default=env("DATABASE_URL", "sqlite:///gateway.db"))
    parser.add_argument("--data-dir", default=env("GATEWAY_DATA_DIR", "gateway_data"))
    parser.add_argument("--validators", default=env("VALIDATORS", ""))
    parser.add_argument("--node-urls", default=env("NODE_URLS", ""))
    parser.add_argument("--chain-urls", default=env("CHAIN_URLS", ""))
    parser.add_argument("--receipts-url", default=env("RECEIPTS_URL", ""))
    parser.add_argument("--receipts-key", default=env("RECEIPT_API_KEY", ""))
    parser.add_argument("--admin-email", default=env("ADMIN_EMAIL", ""))
    parser.add_argument("--admin-password", default=env("ADMIN_PASSWORD", ""))
    parser.add_argument("--index-interval", type=float, default=float(env("INDEX_INTERVAL", 1.0)))
    args = parser.parse_args()
    setup(args)
    threading.Thread(target=indexer_loop, daemon=True).start()
    print(f"gateway on port {args.port}, database {db.kind}, nodes {cfg.node_urls}, chain read from {cfg.chain_urls}")
    app.run(host=args.host, port=args.port, threaded=True)