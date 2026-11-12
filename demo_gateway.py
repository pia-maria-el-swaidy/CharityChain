"""Tells the whole story through the gateway, step by step, with real output. Works on Windows, macOS and Linux.

    python demo_gateway.py --admin-email admin@tracechain.local --admin-password YOUR_ADMIN_PASSWORD
    python demo_gateway.py --gateway http://127.0.0.1:8000 --admin-email ... --admin-password ...

(The administrator's password: the one you put in .env as ADMIN_PASSWORD, or, if you did not, the one printed once
in the gateway log:  docker compose logs gateway)
Every run creates new accounts and a new campaign, so it can be repeated.
"""
import argparse
import hashlib
import secrets
import sys
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--gateway", default="http://127.0.0.1:8000")
ap.add_argument("--admin-email", required=True)
ap.add_argument("--admin-password", required=True)
args = ap.parse_args()
G = args.gateway.rstrip("/")
run = secrets.token_hex(3)


def call(method, path, token=None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.request(method, G + path, headers=headers, timeout=30, **kw)
    return r


def need(r, *codes):
    if r.status_code not in codes:
        sys.exit(f"\nSTOP: {r.request.method} {r.request.path_url} answered {r.status_code}: {r.text[:300]}")
    return r.json() if r.headers.get("Content-Type", "").startswith("application/json") else r


def step(text):
    print(f"\n--- {text}")


def wait_final(tx_id):
    start = time.time()
    while time.time() - start < 30:
        s = need(call("GET", f"/api/transactions/{tx_id}"), 200)
        if s["status"] == "confirmed":
            loc = s["location"]
            print(f"    final after {time.time() - start:.1f} s: block {loc['block']}, signed by {loc['signatures']} of 4 validators")
            return s
        time.sleep(0.2)
    sys.exit(f"STOP: {tx_id} did not become final in 30 s: are the validators running? ({G}/api/health)")


step("0. is the system up?")
health = need(call("GET", "/api/health"), 200)
print(f"    database: {health['database']}, verified chain height: {health['chain']['verified_height']}, "
      f"nodes up: {sum(health['nodes'].values())}/{len(health['nodes'])}, receipt service: {health['receipt_service']}")

step("1. a donor and a charity register; the administrator verifies the charity")
pw = "demo-password-1"
need(call("POST", "/api/auth/register", json={"email": f"ali{run}@example.org", "name": "Ali", "role": "donor", "password": pw}), 201)
need(call("POST", "/api/auth/register", json={"email": f"hope{run}@example.org", "name": f"Hope Foundation {run}", "role": "charity",
                                               "password": pw, "description": "Flood relief: food packages and shelter"}), 201)
donor = need(call("POST", "/api/auth/login", json={"email": f"ali{run}@example.org", "password": pw}), 200)["token"]
charity = need(call("POST", "/api/auth/login", json={"email": f"hope{run}@example.org", "password": pw}), 200)
hope_id, hope_token = charity["user"]["id"], charity["token"]
admin = need(call("POST", "/api/auth/login", json={"email": args.admin_email, "password": args.admin_password}), 200)["token"]
need(call("POST", f"/api/admin/charities/{hope_id}/verify", admin), 200)
print("    registered: Ali (donor) and Hope Foundation (charity, now verified)")

step("2. the charity opens a campaign")
slug = f"flood-{run}"
need(call("POST", "/api/campaigns", hope_token, json={"slug": slug, "title": "Flood relief", "description": "Food and shelter", "goal": 5000}), 201)
print(f"    campaign '{slug}'")

step("3. Ali donates 1000 to the campaign")
d = need(call("POST", "/api/donations", donor, json={"charity_id": hope_id, "amount": 1000, "campaign": slug}), 202)["tx_id"]
print(f"    accepted as {d}: status pending")
wait_final(d)

step("4. the charity sets 600 aside for food packages")
a = need(call("POST", "/api/allocations", hope_token, json={"donation_id": d, "amount": 600, "project": "food-packages"}), 202)["tx_id"]
wait_final(a)

step("5. the charity buys rice for 250 and records the expense with its receipt")
receipt = b"%PDF-1.4\n% demo receipt: 50 kg rice, Market Co, run " + run.encode() + b"\n%%EOF\n"
e = need(call("POST", "/api/expenses", hope_token, data={"allocation_id": a, "amount": "250", "description": "Rice, 50 kg, Market Co"},
              files={"file": (f"invoice-{run}.pdf", receipt, "application/pdf")}), 202)["tx_id"]
wait_final(e)

step("6. Ali asks: where did my money go?   (the gateway verified every block itself)")
t = need(call("GET", f"/api/trace/{d}"), 200)
don = t["donation"]
print(f"    {don['donor_name']} gave {don['amount']} to {don['charity_name']} for campaign '{don['campaign']}'  (block {don['location']['block']})")
print(f"    set aside: {t['allocated']}   not yet allocated: {t['unallocated']}   spent: {t['spent_total']}")
for al in t["allocations"]:
    print(f"    project '{al['project']}': {al['amount']} (spent {al['spent']}, left {al['remaining']})")
    for ex in al["expenses"]:
        print(f"        expense {ex['amount']}: {ex['description']}  receipt {ex['receipt']['filename']}  hash {ex['receipt_hash'][:16]}...")
print(f"    {t['verified']['by']}")

step("7. Ali checks the receipt himself")
v = need(call("GET", f"/api/receipts/{hashlib.sha256(receipt).hexdigest()}/verify"), 200)
got = call("GET", f"/api/receipts/{hashlib.sha256(receipt).hexdigest()}").content
print(f"    on the chain: {v['on_chain']}, stored file matches its hash: {v['file_matches_hash']}, "
      f"downloaded file identical to the original: {got == receipt}")

step("8. the charity tries to spend more than it set aside (500, but only 350 is left)")
r = call("POST", "/api/expenses", hope_token, data={"allocation_id": a, "amount": "500", "description": "Too much"},
         files={"file": ("x.pdf", receipt + b"2", "application/pdf")})
print(f"    refused by the blockchain: {r.status_code} {r.json().get('error')}")

step("9. the charity's public page")
c = need(call("GET", f"/api/charities/{hope_id}"), 200)
print(f"    {c['name']}: donated {c['totals']['donated']}, allocated {c['totals']['allocated']}, spent {c['totals']['spent']}; "
      f"campaign '{c['campaigns'][0]['slug']}' raised {c['campaigns'][0]['raised']} of {c['campaigns'][0]['goal']}")
print("\nDONE: every figure above was verified by the gateway from the validators' signatures.")