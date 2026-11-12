"""End-to-end flow through the gateway, HTTP only (no imports from the chain code).

DONATION -> ALLOCATION -> DISBURSEMENT -> trace, then the rejections:
the gateway refuses bad parents / bad receipt hashes (400), the chain refuses
over-allocation and over-spending (422).

Needs the gateway (GATEWAY_URL, default http://localhost:5000) and the chain nodes running.
Every run creates its own charity, donor and campaign, so runs do not collide.
Exit code 0 = all checks passed, 1 = at least one check failed.
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("GATEWAY_URL", "http://localhost:5000").rstrip("/")
WAIT_SECONDS = int(os.environ.get("E2E_WAIT_SECONDS", "90"))

failures = []


def call(method, path, body=None):
    """Return (http_status, parsed_json). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return ok


def expect_status(name, got, want, body):
    return check(name, got == want, f"expected HTTP {want}, got {got}: {body}")


def wait_committed(tx_id):
    """Poll the gateway until the transaction is COMMITTED (nudging the sync now and then)."""
    deadline = time.time() + WAIT_SECONDS
    polls = 0
    while time.time() < deadline:
        code, row = call("GET", f"/api/transactions/{tx_id}")
        if code == 200 and row.get("status") == "COMMITTED":
            return True
        polls += 1
        if polls % 5 == 0:
            call("POST", "/api/chain/sync")
        time.sleep(2)
    return False


def stop_if_failed():
    if failures:
        print(f"\nStopped early: {len(failures)} check(s) failed.")
        sys.exit(1)


def main():
    tag = secrets.token_hex(4)
    print(f"Gateway: {BASE}   run: {tag}\n")

    # ---- setup ----
    code, charity = call("POST", "/api/charities", {
        "name": f"E2E Charity {tag}", "description": "e2e test", "wallet_ref": f"wallet-e2e-{tag}"})
    expect_status("create charity", code, 201, charity)
    code, donor = call("POST", "/api/donors", {
        "display_name": f"E2E Donor {tag}", "email": f"e2e-{tag}@example.com"})
    expect_status("create donor", code, 201, donor)
    stop_if_failed()
    cid, did = charity["charity_id"], donor["donor_id"]
    code, camp = call("POST", "/api/campaigns", {
        "charity_id": cid, "title": f"E2E campaign {tag}", "goal_amount": 100000, "currency": "USD"})
    expect_status("create campaign", code, 201, camp)
    stop_if_failed()
    camp_id = camp["campaign_id"]
    common = {"currency": "USD", "charity_id": cid, "campaign_id": camp_id}

    # ---- DONATION 10000 ----
    code, don = call("POST", "/api/transactions", {**common, "tx_type": "DONATION", "amount": 10000, "donor_id": did})
    expect_status("DONATION accepted (201)", code, 201, don)
    stop_if_failed()
    check("DONATION committed on chain", wait_committed(don["tx_id"]), "still not COMMITTED")
    stop_if_failed()

    # ---- ALLOCATION 6000 ----
    code, alloc = call("POST", "/api/transactions", {**common, "tx_type": "ALLOCATION", "amount": 6000,
                                                     "parent_tx_id": don["tx_id"]})
    expect_status("ALLOCATION 6000 accepted (201)", code, 201, alloc)
    stop_if_failed()
    check("ALLOCATION committed on chain", wait_committed(alloc["tx_id"]), "still not COMMITTED")
    stop_if_failed()

    # ---- DISBURSEMENT 2500 ----
    receipt = secrets.token_hex(32)  # 64 hex characters
    disb_body = {**common, "tx_type": "DISBURSEMENT", "amount": 2500, "parent_tx_id": alloc["tx_id"],
                 "recipient_ref": f"recipient-e2e-{tag}", "receipt_hash": receipt}
    code, disb = call("POST", "/api/transactions", disb_body)
    expect_status("DISBURSEMENT 2500 accepted (201)", code, 201, disb)
    stop_if_failed()
    check("DISBURSEMENT committed on chain", wait_committed(disb["tx_id"]), "still not COMMITTED")
    stop_if_failed()

    # ---- rejections by the gateway (400) ----
    code, body = call("POST", "/api/transactions", {**common, "tx_type": "ALLOCATION", "amount": 100})
    expect_status("ALLOCATION without parent_tx_id -> 400", code, 400, body)

    code, body = call("POST", "/api/transactions", {**common, "tx_type": "ALLOCATION", "amount": 100,
                                                    "parent_tx_id": alloc["tx_id"]})
    expect_status("ALLOCATION with an ALLOCATION as parent -> 400", code, 400, body)

    code, body = call("POST", "/api/transactions", {**common, "tx_type": "ALLOCATION", "amount": 100,
                                                    "parent_tx_id": "does-not-exist"})
    expect_status("ALLOCATION with unknown parent -> 400", code, 400, body)

    code, body = call("POST", "/api/transactions", {**disb_body, "amount": 100, "parent_tx_id": don["tx_id"]})
    expect_status("DISBURSEMENT with a DONATION as parent -> 400", code, 400, body)

    code, body = call("POST", "/api/transactions", {**disb_body, "amount": 100, "receipt_hash": "abc"})
    expect_status("DISBURSEMENT with bad receipt_hash -> 400", code, 400, body)

    no_recipient = {k: v for k, v in disb_body.items() if k != "recipient_ref"}
    code, body = call("POST", "/api/transactions", {**no_recipient, "amount": 100})
    expect_status("DISBURSEMENT without recipient_ref -> 400", code, 400, body)

    # ---- rejections by the chain (422) ----
    code, body = call("POST", "/api/transactions", {**common, "tx_type": "ALLOCATION", "amount": 5000,
                                                    "parent_tx_id": don["tx_id"]})
    expect_status("over-allocation (6000 + 5000 > 10000) -> 422", code, 422, body)

    code, body = call("POST", "/api/transactions", {**disb_body, "amount": 4000})
    expect_status("over-spend (2500 + 4000 > 6000) -> 422", code, 422, body)

    # ---- trace: the rejected transactions must not have changed anything ----
    code, trace = call("GET", f"/api/donations/{don['tx_id']}/trace")
    expect_status("trace answers 200", code, 200, trace)
    stop_if_failed()
    allocs = trace.get("allocations") or []
    first = allocs[0] if allocs else {}
    expenses = first.get("expenses") or []
    check("trace: donation id and amount", trace.get("donation", {}).get("id") == don["tx_id"]
          and trace["donation"].get("amount") == 10000, trace.get("donation"))
    check("trace: allocated 6000, unallocated 4000", trace.get("allocated") == 6000
          and trace.get("unallocated") == 4000, f"allocated={trace.get('allocated')} unallocated={trace.get('unallocated')}")
    check("trace: exactly one allocation, id matches", len(allocs) == 1 and first.get("id") == alloc["tx_id"], allocs)
    check("trace: allocation spent 2500, remaining 3500", first.get("spent") == 2500
          and first.get("remaining") == 3500, f"spent={first.get('spent')} remaining={first.get('remaining')}")
    check("trace: exactly one expense with our receipt hash", len(expenses) == 1
          and expenses[0].get("id") == disb["tx_id"] and expenses[0].get("receipt_hash") == receipt, expenses)
    check("trace: spent_total 2500", trace.get("spent_total") == 2500, trace.get("spent_total"))

    # ---- rejected requests left nothing behind in the gateway DB ----
    code, rows = call("GET", f"/api/transactions?campaign_id={camp_id}&limit=50")
    expect_status("list transactions of the campaign", code, 200, rows)
    if code == 200:
        check("exactly 3 transactions stored (rejected ones were not kept)", len(rows) == 3, f"found {len(rows)}")
        check("all 3 are COMMITTED", all(r.get("status") == "COMMITTED" for r in rows),
              [r.get("status") for r in rows])

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()