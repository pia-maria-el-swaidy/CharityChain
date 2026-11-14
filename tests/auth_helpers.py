"""Login helpers for the HTTP tests. Needs the gateway running and ADMIN_EMAIL / ADMIN_PASSWORD
(from the environment or from the local .env). Nothing is imported from the gateway."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def env_value(name):
    v = os.environ.get(name)
    if v:
        return v
    f = Path(__file__).resolve().parent.parent / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == name:
                return val.strip().strip('"').strip("'")
    return None


def http(base, method, path, body=None, token=None):
    """Return (status, parsed_json). Never raises on 4xx/5xx."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


def login(base, email, password):
    code, body = http(base, "POST", "/api/auth/login", {"email": email, "password": password})
    assert code == 200, f"login failed for {email}: {code} {body}"
    return body["token"]


def make_actors(base, charity_id, tag):
    """Register a donor, create a CHARITY_ADMIN and a FINANCE_OFFICER of charity_id (via the admin),
    and return their tokens plus the donor's donor_id."""
    admin_email, admin_pw = env_value("ADMIN_EMAIL"), env_value("ADMIN_PASSWORD")
    if not admin_email or not admin_pw:
        raise RuntimeError("ADMIN_EMAIL / ADMIN_PASSWORD not found in the environment or .env")
    pw = f"Pw-{tag}-123456"

    code, reg = http(base, "POST", "/api/auth/register",
                     {"full_name": f"VAL Donor {tag}", "email": f"donor-{tag}@example.com", "password": pw})
    assert code in (200, 201), f"register failed: {code} {reg}"
    donor_token = reg["token"]
    code, me = http(base, "GET", "/api/auth/me", token=donor_token)
    assert code == 200 and me.get("donor_id"), f"/api/auth/me gave no donor_id: {code} {me}"

    admin_token = login(base, admin_email, admin_pw)
    tokens = {}
    for role, key in (("CHARITY_ADMIN", "charity_token"), ("FINANCE_OFFICER", "finance_token")):
        email = f"{role.lower()}-{tag}@example.com"
        code, out = http(base, "POST", "/api/admin/users", {
            "full_name": f"VAL {role} {tag}", "email": email, "password": pw,
            "role": role, "charity_id": charity_id}, token=admin_token)
        assert code in (200, 201), f"admin could not create {role}: {code} {out}"
        tokens[key] = login(base, email, pw)
    return {"donor_id": me["donor_id"], "donor_token": donor_token, **tokens}
