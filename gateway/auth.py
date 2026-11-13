"""Accounts, sign in, sign up and roles for the gateway.

Roles (one blockchain, many roles: a role only decides what a person may ask the gateway to do):
  DONOR            donates and traces their own donations      (self sign-up)
  CHARITY_ADMIN    manages campaigns and allocates donations   (created by a system administrator)
  FINANCE_OFFICER  records expenses with receipts              (created by a system administrator)
  AUDITOR          reads and verifies everything               (created by a system administrator)
  SYSTEM_ADMIN     manages users and the platform              (first one comes from ADMIN_EMAIL / ADMIN_PASSWORD)

Tokens are signed with itsdangerous (already installed with Flask) and expire after 8 hours.
Passwords are hashed with werkzeug (scrypt). No new dependency is needed.

Use  @auth.require_auth("CHARITY_ADMIN")  on any other route to protect it (next step).
"""
import os
import re
import threading
import time
import uuid
from functools import wraps

import psycopg2
from flask import Blueprint, g, jsonify, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from gateway.db import add_donor, get_conn

ROLES = ("DONOR", "CHARITY_ADMIN", "FINANCE_OFFICER", "AUDITOR", "SYSTEM_ADMIN")
STAFF_ROLES = ROLES[1:]
NEEDS_CHARITY = {"CHARITY_ADMIN", "FINANCE_OFFICER"}

TOKEN_MAX_AGE = 8 * 3600
MIN_PASSWORD = 8
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('DONOR','CHARITY_ADMIN','FINANCE_OFFICER','AUDITOR','SYSTEM_ADMIN')),
    donor_id UUID REFERENCES donors(donor_id) ON DELETE RESTRICT,
    charity_id UUID REFERENCES charities(charity_id) ON DELETE RESTRICT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (role <> 'DONOR' OR donor_id IS NOT NULL),
    CHECK (role NOT IN ('CHARITY_ADMIN','FINANCE_OFFICER') OR charity_id IS NOT NULL)
);
"""

bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------- storage (the only code that touches SQL)
def init_auth():
    """Create the users table, check the secret, and create the first system administrator."""
    _serializer()  # fail at start-up, not at the first login, if AUTH_SECRET is missing
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if email and password and not find_by_email(email):
        insert_user(email, "System Administrator", password, "SYSTEM_ADMIN")


def _clean_uuid(value):
    try:
        return str(uuid.UUID(str(value))).lower()
    except (ValueError, TypeError):
        return None


def find_by_email(email):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            return cur.fetchone()


def find_by_id(user_id):
    user_id = _clean_uuid(user_id)
    if not user_id:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()


def insert_user(email, full_name, password, role, donor_id=None, charity_id=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, full_name, password_hash, role, donor_id, charity_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (email, full_name, generate_password_hash(password), role, donor_id, charity_id),
            )
            return cur.fetchone()


def list_users():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY created_at DESC")
            return cur.fetchall()


# ---------------------------------------------------------------- tokens
def _serializer():
    secret = os.environ.get("AUTH_SECRET")
    if not secret or len(secret) < 16:
        raise RuntimeError("AUTH_SECRET must be set to a random string of at least 16 characters (see .env.example)")
    return URLSafeTimedSerializer(secret, salt="charitychain-auth")


def make_token(user):
    return _serializer().dumps({"uid": str(user["user_id"])})


def public_user(user):
    return {
        "user_id": str(user["user_id"]),
        "email": user["email"],
        "full_name": user["full_name"],
        "role": user["role"],
        "donor_id": str(user["donor_id"]) if user.get("donor_id") else None,
        "charity_id": str(user["charity_id"]) if user.get("charity_id") else None,
        "is_active": bool(user.get("is_active", True)),
    }


def current_user():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        data = _serializer().loads(header[7:].strip(), max_age=TOKEN_MAX_AGE)  # expiry is a BadSignature too
    except BadSignature:
        return None
    user = find_by_id(data.get("uid")) if isinstance(data, dict) else None
    return user if user and user["is_active"] else None


def require_auth(*roles):
    """Protect a route. With no roles: any signed-in user. With roles: only those roles."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return jsonify({"error": "sign in required"}), 401
            if roles and user["role"] not in roles:
                return jsonify({"error": "your role is not allowed to do this"}), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------- slow-down for password guessing
_FAILS, _LOCK = {}, threading.Lock()
MAX_FAILS, WINDOW = 5, 300


def _throttled(key):
    now = time.time()
    with _LOCK:
        _FAILS[key] = [t for t in _FAILS.get(key, []) if now - t < WINDOW]
        return len(_FAILS[key]) >= MAX_FAILS


def _record_fail(key):
    with _LOCK:
        _FAILS.setdefault(key, []).append(time.time())


def _clear_fails(key):
    with _LOCK:
        _FAILS.pop(key, None)


DUMMY_HASH = generate_password_hash("not-a-real-password")  # so unknown emails take as long as wrong passwords


# ---------------------------------------------------------------- routes
def _credentials(data, need_name):
    name = str(data.get("full_name") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    problems = {}
    if need_name and len(name) < 2:
        problems["full_name"] = "Enter your full name."
    if not EMAIL_RE.match(email):
        problems["email"] = "Enter a valid email address."
    if need_name and len(password) < MIN_PASSWORD:
        problems["password"] = f"Use at least {MIN_PASSWORD} characters."
    return name, email, password, problems


@bp.post("/api/auth/register")
def register():
    """Public sign-up. Always creates a DONOR, never a staff role."""
    name, email, password, problems = _credentials(request.get_json(silent=True) or {}, need_name=True)
    if problems:
        return jsonify({"error": "Check the highlighted fields.", "fields": problems}), 400
    if find_by_email(email):
        return jsonify({"error": "An account with this email already exists.", "fields": {"email": "Already registered."}}), 409
    try:
        donor_id = add_donor(name, email, False)   # creates the donor and its custodial blockchain wallet
        user = insert_user(email, name, password, "DONOR", donor_id=donor_id)
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "An account with this email already exists.", "fields": {"email": "Already registered."}}), 409
    return jsonify({"token": make_token(user), "user": public_user(user)}), 201


@bp.post("/api/auth/login")
def login():
    _, email, password, problems = _credentials(request.get_json(silent=True) or {}, need_name=False)
    if problems.get("email") or not password:
        return jsonify({"error": "Enter your email and password."}), 400
    key = (email, request.remote_addr)
    if _throttled(key):
        return jsonify({"error": "Too many failed attempts. Wait a few minutes and try again."}), 429
    user = find_by_email(email)
    ok = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
    if not user or not ok or not user["is_active"]:
        _record_fail(key)
        return jsonify({"error": "Incorrect email or password."}), 401
    _clear_fails(key)
    return jsonify({"token": make_token(user), "user": public_user(user)})


@bp.get("/api/auth/me")
@require_auth()
def me():
    return jsonify(public_user(g.user))


@bp.get("/api/admin/users")
@require_auth("SYSTEM_ADMIN")
def admin_list_users():
    return jsonify([public_user(u) for u in list_users()])


@bp.post("/api/admin/users")
@require_auth("SYSTEM_ADMIN")
def admin_create_user():
    """A system administrator creates staff accounts."""
    data = request.get_json(silent=True) or {}
    name, email, password, problems = _credentials(data, need_name=True)
    role = data.get("role")
    if role not in STAFF_ROLES:
        problems["role"] = f"Role must be one of {', '.join(STAFF_ROLES)}."
    charity_id = _clean_uuid(data.get("charity_id")) if data.get("charity_id") else None
    if role in NEEDS_CHARITY and not charity_id:
        problems["charity_id"] = "Choose the charity this person works for."
    if problems:
        return jsonify({"error": "Check the highlighted fields.", "fields": problems}), 400
    if find_by_email(email):
        return jsonify({"error": "An account with this email already exists.", "fields": {"email": "Already registered."}}), 409
    try:
        user = insert_user(email, name, password, role, charity_id=charity_id if role in NEEDS_CHARITY else None)
    except psycopg2.errors.ForeignKeyViolation:
        return jsonify({"error": "Unknown charity.", "fields": {"charity_id": "Unknown charity."}}), 400
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "An account with this email already exists.", "fields": {"email": "Already registered."}}), 409
    return jsonify(public_user(user)), 201