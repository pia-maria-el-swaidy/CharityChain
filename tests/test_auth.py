"""Tests for gateway/auth.py. No database needed: the four storage functions are replaced by an in-memory fake.
Run from the project root:   python -m pytest tests/test_auth.py -q
"""
import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")   # db.py insists on it at import time
os.environ["AUTH_SECRET"] = "test-secret-with-more-than-16-chars"
os.environ.setdefault("KEY_ENCRYPTION_SECRET", "x")

import pytest
from flask import Flask
from werkzeug.security import generate_password_hash

from gateway import auth


@pytest.fixture()
def client(monkeypatch):
    users = {}

    def insert_user(email, full_name, password, role, donor_id=None, charity_id=None):
        row = {"user_id": uuid.uuid4(), "email": email, "full_name": full_name,
               "password_hash": generate_password_hash(password), "role": role,
               "donor_id": donor_id, "charity_id": charity_id, "is_active": True}
        users[email] = row
        return row

    monkeypatch.setattr(auth, "find_by_email", lambda e: users.get(e))
    monkeypatch.setattr(auth, "find_by_id", lambda i: next((u for u in users.values() if str(u["user_id"]) == str(i)), None))
    monkeypatch.setattr(auth, "insert_user", insert_user)
    monkeypatch.setattr(auth, "list_users", lambda: list(users.values()))
    monkeypatch.setattr(auth, "add_donor", lambda name, email, anon: uuid.uuid4())
    auth._FAILS.clear()
    app = Flask(__name__)
    app.register_blueprint(auth.bp)
    c = app.test_client()
    c.users = users
    return c


def register(c, email="ana@example.com", password="correct horse", name="Ana Khoury"):
    return c.post("/api/auth/register", json={"full_name": name, "email": email, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_creates_donor_and_signs_in(client):
    r = register(client)
    assert r.status_code == 201
    body = r.get_json()
    assert body["user"]["role"] == "DONOR" and body["user"]["donor_id"]
    assert "password" not in str(body).lower().replace("password_hash", "")
    me = client.get("/api/auth/me", headers=bearer(body["token"]))
    assert me.status_code == 200 and me.get_json()["email"] == "ana@example.com"


def test_register_cannot_choose_a_role(client):
    r = client.post("/api/auth/register", json={"full_name": "Eve Hacker", "email": "e@x.io", "password": "longenough1", "role": "SYSTEM_ADMIN"})
    assert r.get_json()["user"]["role"] == "DONOR"


def test_register_validation(client):
    assert register(client, email="not-an-email").status_code == 400
    r = register(client, password="short")
    assert r.status_code == 400 and "password" in r.get_json()["fields"]
    assert register(client, name="A").status_code == 400


def test_duplicate_email_is_409_and_case_insensitive(client):
    assert register(client).status_code == 201
    assert register(client, email="ANA@example.com").status_code == 409


def test_login_ok_and_wrong_password(client):
    register(client)
    ok = client.post("/api/auth/login", json={"email": "Ana@Example.com", "password": "correct horse"})
    assert ok.status_code == 200 and ok.get_json()["token"]
    bad = client.post("/api/auth/login", json={"email": "ana@example.com", "password": "wrong password"})
    unknown = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "whatever123"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.get_json() == unknown.get_json()          # same message: do not reveal which emails exist


def test_login_is_throttled_after_five_failures(client):
    register(client)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "ana@example.com", "password": "nope-nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "ana@example.com", "password": "correct horse"})
    assert r.status_code == 429


def test_me_needs_a_valid_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=bearer("garbage")).status_code == 401


def test_expired_token_is_refused(client, monkeypatch):
    token = register(client).get_json()["token"]
    monkeypatch.setattr(auth, "TOKEN_MAX_AGE", -1)
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_deactivated_user_loses_access(client):
    token = register(client).get_json()["token"]
    client.users["ana@example.com"]["is_active"] = False
    assert client.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_admin_endpoints_need_system_admin(client):
    donor_token = register(client).get_json()["token"]
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/admin/users", headers=bearer(donor_token)).status_code == 403
    assert client.post("/api/admin/users", headers=bearer(donor_token), json={}).status_code == 403


def test_admin_creates_staff(client):
    auth.insert_user("root@x.io", "Root", "adminpass123", "SYSTEM_ADMIN")
    tok = client.post("/api/auth/login", json={"email": "root@x.io", "password": "adminpass123"}).get_json()["token"]
    h = bearer(tok)
    charity = str(uuid.uuid4())
    good = client.post("/api/admin/users", headers=h, json={"full_name": "Fadi Haddad", "email": "fadi@x.io", "password": "financepass1", "role": "FINANCE_OFFICER", "charity_id": charity})
    assert good.status_code == 201 and good.get_json()["charity_id"] == charity
    assert client.post("/api/admin/users", headers=h, json={"full_name": "No Charity", "email": "n@x.io", "password": "financepass1", "role": "CHARITY_ADMIN"}).status_code == 400
    assert client.post("/api/admin/users", headers=h, json={"full_name": "Bad Role", "email": "b@x.io", "password": "financepass1", "role": "DONOR"}).status_code == 400
    assert len(client.get("/api/admin/users", headers=h).get_json()) == 2


def test_require_auth_blocks_other_roles(client):
    app = client.application

    @app.get("/only-finance")
    @auth.require_auth("FINANCE_OFFICER")
    def only_finance():
        return {"ok": True}

    donor = register(client).get_json()["token"]
    assert client.get("/only-finance", headers=bearer(donor)).status_code == 403
    auth.insert_user("fo@x.io", "Fin Off", "financepass1", "FINANCE_OFFICER", charity_id=str(uuid.uuid4()))
    fo = client.post("/api/auth/login", json={"email": "fo@x.io", "password": "financepass1"}).get_json()["token"]
    assert client.get("/only-finance", headers=bearer(fo)).status_code == 200


def test_missing_secret_fails_fast(monkeypatch):
    monkeypatch.delenv("AUTH_SECRET")
    with pytest.raises(RuntimeError):
        auth._serializer()